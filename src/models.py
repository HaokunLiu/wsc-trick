import torch
import torch.nn as nn
import torch.nn.functional as F
import transformers


class RobertaWSCModel(nn.Module):
    def __init__(self, framing, roberta_model="roberta-large"):
        """
        framing, one of
            "P-SPAN"
            "P-SENT"
            "MC-SENT-PLOSS"
            "MC-SENT-NOSCALE"
            "MC-SENT-NOPAIR"
            "MC-SENT"
            "MC-MLM"
        """
        super().__init__()

        self.pad_logits = -100

        self.tokenizer = transformers.RobertaTokenizer.from_pretrained(roberta_model)
        self.mask_token_id = self.tokenizer.mask_token_id
        self.pad_token_id = self.tokenizer.pad_token_id

        transformer_with_lm = transformers.RobertaForMaskedLM.from_pretrained(roberta_model)
        self.hidden_size = transformer_with_lm.config.hidden_size
        self.framing = framing

        self.transformer = transformer_with_lm.roberta
        if "-SPAN-" in self.framing:
            self.span_head = nn.Linear(3 * self.hidden_size, 1)
        elif "-SENT-" in self.framing:
            self.sent_head = nn.Linear(self.hidden_size, 1)
        elif "-MLM-" in self.framing:
            self.mlm_head = transformer_with_lm.lm_head

    def forward(self, batch_inputs):
        """
        batch_inputs:
            for SPAN input
            "raw_input": (bs, seq_len)
            "span1_mask": (bs, seq_len)
            "span2_mask": (bs, seq_len)
            for SENT and MLM input
            "query_input": (bs, seq_len)
            "cand_input": (bs, max_cands, seq_len)
            for MLM input
            "mask_query_input": (bs, seq_len)
            "mask_cand_input": (bs, max_cands, seq_len)
            for P loss
            "p_label": (bs,)
            for MC loss
            "mc_label": (bs,) between [0, max_cands]
        batch_outputs:
            "loss": (1,)
            "acc": (1,)
            "query_pred": (bs,)
        """
        if "-SPAN-" in self.framing:
            assert self.framing.startswith("P-")
            raw_repr = self.transformer(batch_inputs["raw_input"])
            cls_repr = raw_repr[:, 0]
            span1_mask = batch_inputs["span1_mask"].unsqueeze(dim=2)
            span1_repr = torch.sum(raw_repr * span1_mask, dim=1) / span1_mask.sum(dim=1)
            span2_repr = torch.sum(
                raw_repr * batch_inputs["span2_mask"].unsqueeze(dim=2), dim=1
            ) / batch_inputs["span2_mask"].sum(dim=1, keepdim=True)
            concat_repr = torch.cat([cls_repr, span1_repr, span2_repr], dim=2)
            query_logits = self.span_head(concat_repr)

        elif "-SENT-" in self.framing:
            query_repr = self.transformer(batch_inputs["query_input"])[:, 0]
            query_logits = self.sent_head(query_repr)

            if self.framing.startswith("MC-"):
                valid_cand_mask = (batch_inputs["cand_input"] != self.pad_token_id).max(dim=2)[0]
                cand_input = batch_inputs["cand_input"][valid_cand_mask]
                cand_repr = self.transformer(cand_input)[:, 0]
                cand_logits = self.sent_head(cand_repr)

        elif "-MLM-" in self.framing:
            assert self.framing.startswith("MC-")
            query_repr = self.transformer(batch_inputs["mask_query_input"])
            query_prob = torch.gather(
                F.log_softmax(self.mlm_head(query_repr), dim=2),
                index=batch_inputs["query_input"].unsqueeze(2),
                dim=2,
            )
            query_mask = (
                (batch_inputs["mask_query_input"] == self.mask_token_id).float().unsqueeze(dim=2)
            )
            query_logits = torch.sum(query_prob * query_mask, dim=1) / query_mask.sum(dim=1)

            valid_cand_mask = (batch_inputs["cand_input"] != self.pad_token_id).max(dim=2)[0]
            mask_cand_input = batch_inputs["mask_cand_input"][valid_cand_mask]
            cand_repr = self.transformer(mask_cand_input)
            cand_prob = torch.gather(
                F.log_softmax(self.mlm_head(cand_repr), dim=2),
                index=batch_inputs["cand_input"].unsqueeze(2),
                dim=2,
            )
            cand_mask = (
                (batch_inputs["mask_cand_input"] == self.mask_token_id).float().unsqueeze(dim=2)
            )
            cand_logits = torch.sum(cand_prob * cand_mask, dim=1) / cand_mask.sum(dim=1)

        # query_logits: (bs,)
        if self.framing.startswith("MC-"):
            # cand_logits: (batch_cand_count,)
            full_cand_logits = torch.ones_like(valid_cand_mask.float()) * self.pad_logits
            full_cand_logits[valid_cand_mask] = cand_logits
            # full_cand_logits: (bs, max_cands)

        if self.train:
            if self.framing in ["P-SPAN", "P-SENT", "MC-SENT-PLOSS"]:
                loss = F.binary_cross_entropy_with_logits(query_logits, batch_inputs["p_label"])
            elif self.framing in ["MC-SENT", "MC-MLM"]:
                loss = F.cross_entropy(
                    torch.cat([query_logits.unsqueeze(dim=1), cand_logits], dim=1),
                    batch_inputs["mc_label"],
                )
            elif self.framing == "MC-SENT-NOSCALE":
                concat_logits = (
                    torch.cat([query_logits.unsqueeze(dim=1), cand_logits.detach()], dim=1),
                ).flatten()
                one_hot_label = (
                    torch.zeros_like(concat_logits)
                    .scatter_(dim=1, index=batch_inputs["mc_label"], src=1)
                    .flatten()
                )
                non_pad_mask = concat_logits != self.pad_logits
                loss = F.binary_cross_entropy_with_logits(
                    concat_logits[non_pad_mask], one_hot_label[non_pad_mask]
                )
            elif self.framing == "MC-SENT-NOPAIR":
                loss = F.cross_entropy(
                    torch.cat([query_logits.unsqueeze(dim=1), cand_logits.detach()], dim=1),
                    batch_inputs["mc_label"],
                )

        if self.framing.startswith("P-"):
            query_pred = query_logits > 0
        elif self.framing.startswith("MC-"):
            query_pred = query_logits > (cand_logits.max(dim=1)[0])
        acc = (query_pred == batch_inputs["p_label"]).float().mean()

        batch_outputs = {"loss": loss, "label_pred": query_pred, "acc": acc}
        return batch_outputs