from typing import List, Union

import torch

from sglang.srt.managers.schedule_batch import Modality, MultimodalDataItem
from sglang.srt.models.minicpmo import MiniCPMO
from sglang.srt.models.minicpmv import MiniCPMV
from sglang.srt.multimodal.processors.base_processor import (
    BaseMultimodalProcessor,
    MultimodalSpecialTokens,
)


# Compatible with both 'O' and 'V'
class MiniCPMMultimodalProcessor(BaseMultimodalProcessor):
    models = [MiniCPMV, MiniCPMO]

    def __init__(self, hf_config, server_args, _processor, *args, **kwargs):
        super().__init__(hf_config, server_args, _processor, *args, **kwargs)
        # Collect special token ids
        tokenizer = self._processor.tokenizer
        self.slice_start_id = getattr(tokenizer, "slice_start_id", None)
        self.slice_end_id = getattr(tokenizer, "slice_end_id", None)
        self.audio_start_id = getattr(tokenizer, "audio_start_id", None)
        self.audio_end_id = getattr(tokenizer, "audio_end_id", None)
        self.im_start_id = getattr(tokenizer, "im_start_id", None)
        self.im_end_id = getattr(tokenizer, "im_end_id", None)
        self.im_token_id = getattr(tokenizer, "unk_id", None)
        self.mm_tokens = MultimodalSpecialTokens(
            image_token="(<image>./</image>)",
            audio_token="(<audio>./</audio>)",
            video_token="(<video>./</video>)",
            image_token_id=self.im_token_id,
        ).build(_processor)

    async def process_mm_data_async(
        self,
        image_data: List[Union[str, bytes]],
        audio_data: List[Union[str, bytes]],
        input_text,
        request_obj,
        **kwargs,
    ):
        base_output = self.load_mm_data(
            prompt=input_text,
            audio_data=audio_data,
            image_data=image_data,
            multimodal_tokens=self.mm_tokens,
        )
        if base_output is None:
            return None

        res = self.process_mm_data(
            input_text=base_output.input_text,
            images=base_output.images,
            audios=base_output.audios,
        )

        pixel_values = res["pixel_values"]
        tgt_sizes = res["tgt_sizes"]

        if not isinstance(pixel_values, (torch.Tensor, list)):
            raise ValueError(
                "Incorrect type of pixel values. " f"Got type: {type(pixel_values)}"
            )

        if not isinstance(tgt_sizes, (torch.Tensor, list)):
            raise ValueError(
                "Incorrect type of target sizes. " f"Got type: {type(tgt_sizes)}"
            )

        if len(pixel_values) != len(tgt_sizes):
            raise ValueError(
                "Inconsistent batch lengths, found: "
                f"{len(pixel_values)} vs. {len(tgt_sizes)}"
            )

        pixel_values_flat: List[torch.Tensor] = []
        tgt_sizes_flat: List[torch.Tensor] = []
        for pixel_b, tgt_b in zip(pixel_values, tgt_sizes):
            # per image
            if len(pixel_b) != len(tgt_b):
                raise ValueError(
                    "Inconsistent N lengths, found: " f"{len(pixel_b)} vs {len(tgt_b)}"
                )
            for pixel_n, tgt_n in zip(pixel_b, tgt_b):
                pixel_values_flat += [pixel_n]
                tgt_sizes_flat += [tgt_n]

        pixel_values = pixel_values_flat

        items = []
        input_ids = res["input_ids"].flatten()
        image_offsets = self.get_mm_items_offset_by_pair(
            input_ids=input_ids, mm_start_id=self.im_start_id, mm_end_id=self.im_end_id
        )
        slice_offsets = self.get_mm_items_offset_by_pair(
            input_ids=input_ids,
            mm_start_id=self.slice_start_id,
            mm_end_id=self.slice_end_id,
        )
        image_offsets.extend(slice_offsets)
        image_offsets = sorted(image_offsets)

        if len(pixel_values) != 0:
            item = MultimodalDataItem(
                feature=pixel_values,
                offsets=image_offsets,
                model_specific_data={"tgt_size": tgt_sizes_flat},
                modality=Modality.IMAGE,
            )
            items += [item]

        if (
            "audio_features" in res
            and res["audio_features"] is not None
            and len(res["audio_features"]) != 0
        ):
            if self.audio_start_id is not None and self.audio_end_id is not None:
                audio_offsets = self.get_mm_items_offset_by_pair(
                    input_ids=input_ids,
                    mm_start_id=self.audio_start_id,
                    mm_end_id=self.audio_end_id,
                )
            else:
                audio_offsets = None
            item = MultimodalDataItem(
                feature=[res["audio_features"]],
                model_specific_data={"audio_feature_lens": res["audio_feature_lens"]},
                offsets=audio_offsets,
                modality=Modality.AUDIO,
            )
            items += [item]
        return {
            "mm_items": items,
            "input_ids": input_ids.tolist(),
            "audio_start_id": self.audio_start_id,
            "audio_end_id": self.audio_end_id,
            "im_token_id": self.im_token_id,
            "im_start_id": self.im_start_id,
            "im_end_id": self.im_end_id,
            "slice_start_id": self.slice_start_id,
            "slice_end_id": self.slice_end_id,
        }
