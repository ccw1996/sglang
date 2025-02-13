# TODO: also move pad_input_ids into this module
import asyncio
import concurrent.futures
import logging
import multiprocessing as mp
import os
from abc import ABC, abstractmethod
from typing import List, Optional, Union

import numpy as np
import transformers
from decord import VideoReader, cpu
from PIL import Image

from sglang.srt.hf_transformers_utils import get_processor
from sglang.srt.mm_utils import expand2square, process_anyres_image
from sglang.srt.server_args import ServerArgs
from sglang.srt.utils import load_image
from sglang.utils import get_exception_traceback

import torch

import contextlib
from transformers import AutoProcessor
from sglang.srt.managers.image_preprocessors.processing_deepseek_vl_v2 import DeepseekVLV2Processor

logger = logging.getLogger(__name__)

# register deepseek-vl2 preprocess
with contextlib.suppress(ValueError):
    AutoProcessor.register("DeepseekVLV2Processor", DeepseekVLV2Processor)

global global_processor


def init_global_processor(server_args: ServerArgs):
    """Init the global processor for multi modal models."""
    global global_processor
    transformers.logging.set_verbosity_error()
    global_processor = get_processor(
        server_args.tokenizer_path,
        tokenizer_mode=server_args.tokenizer_mode,
        trust_remote_code=server_args.trust_remote_code,
    )


class BaseImageProcessor(ABC):
    def __init__(self, hf_config, server_args, _processor):
        self.hf_config = hf_config
        self._processor = _processor
        self.server_args = server_args

        self.executor = concurrent.futures.ProcessPoolExecutor(
            initializer=init_global_processor,
            mp_context=mp.get_context("fork"),
            initargs=(server_args,),
            max_workers=int(os.environ.get(
                "SGLANG_CPU_COUNT", os.cpu_count())),
        )

    @abstractmethod
    async def process_images_async(self, image_data, input_text, **kwargs):
        pass


class DummyImageProcessor(BaseImageProcessor):
    def __init__(self):
        pass

    async def process_images_async(self, *args, **kwargs):
        return None


class LlavaImageProcessor(BaseImageProcessor):
    def __init__(self, hf_config, server_args, _processor):
        super().__init__(hf_config, server_args, _processor)

    @staticmethod
    def _process_single_image_task(
        image_data: Union[str, bytes],
        image_aspect_ratio: Optional[str] = None,
        image_grid_pinpoints: Optional[str] = None,
        image_processor=None,
    ):
        image_processor = image_processor or global_processor.image_processor

        try:
            image, image_size = load_image(image_data)
            if image_size is not None:
                # It is a video with multiple images
                image_hash = hash(image_data)
                pixel_values = image_processor(image)["pixel_values"]
                for _ in range(len(pixel_values)):
                    pixel_values[_] = pixel_values[_].astype(np.float16)
                pixel_values = np.stack(pixel_values, axis=0)
                return pixel_values, image_hash, image_size
            else:
                # It is an image
                image_hash = hash(image_data)
                if image_aspect_ratio == "pad":
                    image = expand2square(
                        image,
                        tuple(int(x * 255)
                              for x in image_processor.image_mean),
                    )
                    pixel_values = image_processor(image.convert("RGB"))[
                        "pixel_values"
                    ][0]
                elif image_aspect_ratio == "anyres" or (
                    image_aspect_ratio is not None
                    and "anyres_max" in image_aspect_ratio
                ):
                    pixel_values = process_anyres_image(
                        image, image_processor, image_grid_pinpoints
                    )
                else:
                    pixel_values = image_processor(image)["pixel_values"][0]

                if isinstance(pixel_values, np.ndarray):
                    pixel_values = pixel_values.astype(np.float16)

                return pixel_values, image_hash, image.size
        except Exception:
            logger.error("Exception in TokenizerManager:\n" +
                         get_exception_traceback())

    async def _process_single_image(
        self, image_data: Union[bytes, str], aspect_ratio: str, grid_pinpoints: str
    ):
        if self.executor is not None:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self.executor,
                LlavaImageProcessor._process_single_image_task,
                image_data,
                aspect_ratio,
                grid_pinpoints,
            )
        else:
            return self._process_single_image_task(
                image_data, aspect_ratio, grid_pinpoints
            )

    async def process_images_async(
        self,
        image_data: List[Union[str, bytes]],
        input_text,
        request_obj,
        *args,
        **kwargs,
    ):
        if not image_data:
            return None

        modalities = request_obj.modalities or ["image"]
        aspect_ratio = getattr(self.hf_config, "image_aspect_ratio", None)
        grid_pinpoints = (
            self.hf_config.image_grid_pinpoints
            if hasattr(self.hf_config, "image_grid_pinpoints")
            and "anyres" in aspect_ratio
            else None
        )

        if isinstance(image_data, str):
            image_data = [image_data]

        if isinstance(image_data, list) and len(image_data) > 0:
            if "multi-images" in modalities or "video" in modalities:
                # Multiple images
                aspect_ratio = "pad"  # LLaVA OneVision Handling: more than one image --> interleaved image mode or video mode. We do not use anyres
                pixel_values, image_hashes, image_sizes = [], [], []
                res = []
                for img_data in image_data:
                    res.append(
                        self._process_single_image(
                            img_data, aspect_ratio, grid_pinpoints
                        )
                    )
                res = await asyncio.gather(*res)
                for pixel_v, image_h, image_s in res:
                    pixel_values.append(pixel_v)
                    image_hashes.append(image_h)
                    image_sizes.append(image_s)

                if isinstance(pixel_values[0], np.ndarray):
                    pixel_values = np.stack(pixel_values, axis=0)
            else:
                # A single image
                pixel_values, image_hash, image_size = await self._process_single_image(
                    image_data[0], aspect_ratio, grid_pinpoints
                )
                image_hashes = [image_hash]
                image_sizes = [image_size]
        else:
            raise ValueError(f"Invalid image data: {image_data}")

        return {
            "pixel_values": pixel_values,
            "image_hashes": image_hashes,
            "image_sizes": image_sizes,
            "modalities": request_obj.modalities or ["image"],
        }


class MllamaImageProcessor(BaseImageProcessor):
    def __init__(self, hf_config, server_args, _processor):
        super().__init__(hf_config, server_args, _processor)

    @staticmethod
    def _process_single_image_task(images, input_text):
        # input_ids', 'attention_mask', 'pixel_values', 'aspect_ratio_ids', 'aspect_ratio_mask', 'cross_attention_mask'
        return global_processor(images, input_text, return_tensors="pt")

    async def _process_single_image(self, images, input_text):
        if self.executor is not None:
            loop = asyncio.get_event_loop()
            image_inputs = await loop.run_in_executor(
                self.executor,
                MllamaImageProcessor._process_single_image_task,
                images,
                input_text,
            )
        else:
            image_inputs = self._processor(
                images, input_text, return_tensors="pt")

        return image_inputs

    async def process_images_async(
        self, image_data: List[Union[str, bytes]], input_text, *args, **kwargs
    ):
        if not image_data:
            return None

        if isinstance(input_text, list):
            assert len(input_text) and isinstance(input_text[0], int)
            input_text = self._processor.tokenizer.decode(input_text)

        if not isinstance(image_data, list):
            image_data = [image_data]

        if len(image_data) > 0:
            images = [load_image(image)[0] for image in image_data]
        else:
            images = load_image(image_data[0])[0]

        image_inputs = await self._process_single_image(images, input_text)
        image_inputs["image_hashes"] = [hash(str(image_data))]
        image_inputs["input_ids"] = image_inputs["input_ids"].tolist()[0]

        return image_inputs


class MiniCPMVImageProcessor(BaseImageProcessor):
    def __init__(self, hf_config, server_args, _processor):
        super().__init__(hf_config, server_args, _processor)
        self.IMAGE_TOKEN = "(<image>./</image>)"

    @staticmethod
    def _process_images_task(images, input_text):
        result = global_processor.__call__(
            text=input_text, images=images, return_tensors="pt"
        )
        return {
            "input_ids": result["input_ids"],
            "pixel_values": result["pixel_values"],
            "tgt_sizes": result["tgt_sizes"],
        }

    async def _process_images(self, images, input_text):
        if self.executor is not None:
            loop = asyncio.get_event_loop()
            image_inputs = await loop.run_in_executor(
                self.executor,
                MiniCPMVImageProcessor._process_images_task,
                images,
                input_text,
            )
        else:
            image_inputs = self._processor(
                images=images, text=input_text, return_tensors="pt"
            )

        return image_inputs

    async def process_images_async(
        self,
        image_data: List[Union[str, bytes]],
        input_ids,
        request_obj,
        max_req_input_len,
    ):
        if not image_data:
            return None

        if not isinstance(image_data, list):
            image_data = [image_data]

        image_hashes, image_sizes = [], []
        all_frames = []

        # roughly calculate the max number of frames under the max_req_input_len limit
        def calculate_max_num_frames() -> int:
            # Model-specific
            NUM_TOKEN_PER_FRAME = 330

            ret = (max_req_input_len - len(input_ids)) // NUM_TOKEN_PER_FRAME
            return min(ret, 100)

        MAX_NUM_FRAMES = calculate_max_num_frames()

        # print(f"MAX_NUM_FRAMES: {MAX_NUM_FRAMES}")

        def get_estimated_frames_list():
            """
            estimate the total frame count from all visual input
            """
            # Before processing inputs
            estimated_frames_list = []
            for image in image_data:
                if isinstance(image, str) and image.startswith("video:"):
                    path = image[len("video:"):]
                    # Estimate frames for the video
                    vr = VideoReader(path, ctx=cpu(0))
                    num_frames = len(vr)
                else:
                    # For images, each contributes one frame
                    num_frames = 1
                estimated_frames_list.append(num_frames)

            return estimated_frames_list

        estimated_frames_list = get_estimated_frames_list()
        total_frame_count = sum(estimated_frames_list)
        scaling_factor = min(1.0, MAX_NUM_FRAMES / total_frame_count)

        def encode_video(video_path, frame_count_limit=None):
            if not os.path.exists(video_path):
                logger.error(f"Video {video_path} does not exist")
                return []

            if frame_count_limit == 0:
                return []

            def uniform_sample(l, n):
                gap = len(l) / n
                idxs = [int(i * gap + gap / 2) for i in range(n)]
                return [l[i] for i in idxs]

            vr = VideoReader(video_path, ctx=cpu(0))
            sample_fps = round(vr.get_avg_fps() / 1)  # FPS
            frame_idx = [i for i in range(0, len(vr), sample_fps)]
            if frame_count_limit is not None and len(frame_idx) > frame_count_limit:
                frame_idx = uniform_sample(frame_idx, frame_count_limit)
            frames = vr.get_batch(frame_idx).asnumpy()
            frames = [Image.fromarray(v.astype("uint8")) for v in frames]
            return frames

        if isinstance(input_ids, list):
            assert len(input_ids) and isinstance(input_ids[0], int)
            input_text = self._processor.tokenizer.decode(input_ids)
        else:
            input_text = input_ids
        # MiniCPMV requires each frame of video as a single image token
        text_parts = input_text.split(self.IMAGE_TOKEN)
        new_text_parts = []

        # Process each input with allocated frames
        for image_index, (image, estimated_frames) in enumerate(
            zip(image_data, estimated_frames_list)
        ):
            if len(all_frames) >= MAX_NUM_FRAMES:
                frames_to_process = 0
            else:
                frames_to_process = max(
                    1, int(estimated_frames * scaling_factor))

            if frames_to_process == 0:
                frames = []
            else:
                try:
                    if isinstance(image, str) and image.startswith("video:"):
                        path = image[len("video:"):]
                        frames = encode_video(
                            path, frame_count_limit=frames_to_process)
                    else:
                        raw_image, _size = load_image(image)
                        frames = [raw_image]
                    if len(frames) == 0:
                        continue
                except FileNotFoundError as e:
                    print(e)
                    return None
                image_sizes += frames[0].size * len(frames)
                image_hashes += [hash(image)] * len(frames)
                all_frames += frames

            assert frames_to_process == len(frames)

            new_text_parts.append(text_parts[image_index])

            if frames_to_process != 0:
                new_text_parts.append(self.IMAGE_TOKEN * len(frames))

        new_text_parts.append(text_parts[-1])

        input_text = "".join(new_text_parts)

        if len(all_frames) == 0:
            return None
        res = await self._process_images(images=all_frames, input_text=input_text)
        pixel_values = res["pixel_values"]
        tgt_sizes = res["tgt_sizes"]
        input_ids = res["input_ids"]

        # Collect special token ids
        tokenizer = self._processor.tokenizer
        im_start_id = [tokenizer.im_start_id]
        im_end_id = [tokenizer.im_end_id]
        if tokenizer.slice_start_id:
            slice_start_id = [tokenizer.slice_start_id]
            slice_end_id = [tokenizer.slice_end_id]
        return {
            "input_ids": input_ids.flatten().tolist(),
            "pixel_values": pixel_values,
            "tgt_sizes": tgt_sizes,
            "image_hashes": image_hashes,
            "modalities": request_obj.modalities or ["image"],
            "im_start_id": im_start_id,
            "im_end_id": im_end_id,
            "slice_start_id": slice_start_id,
            "slice_end_id": slice_end_id,
        }


class Qwen2VLImageProcessor(BaseImageProcessor):
    def __init__(self, hf_config, server_args, _image_processor):
        self.hf_config = hf_config
        self._image_processor = _image_processor
        self.executor = concurrent.futures.ProcessPoolExecutor(
            initializer=init_global_processor,
            mp_context=mp.get_context("fork"),
            initargs=(server_args,),
            max_workers=int(os.environ.get(
                "SGLANG_CPU_COUNT", os.cpu_count())),
        )

    @staticmethod
    def _process_single_image_task(
        image_data: Union[str, bytes],
        image_processor=None,
    ):
        image_processor = image_processor or global_processor.image_processor

        try:
            image, image_size = load_image(image_data)
            if image_size is not None:
                # It is a video with multiple images
                image_hash = hash(image_data)
                process_result = image_processor(image)
                pixel_values, image_grid_thws = (
                    process_result["pixel_values"],
                    process_result["image_grid_thw"][0],
                )
                for _ in range(len(pixel_values)):
                    pixel_values[_] = pixel_values[_].astype(np.float16)
                pixel_values = np.stack(pixel_values, axis=0)
                image_grid_thws = np.stack(image_grid_thws, axis=0)
                return pixel_values, image_hash, image_size, image_grid_thws
            else:
                # It is an image
                image_hash = hash(image_data)
                process_result = image_processor(image)
                pixel_values, image_grid_thws = (
                    process_result["pixel_values"],
                    process_result["image_grid_thw"][0],
                )
                if isinstance(pixel_values, np.ndarray):
                    pixel_values = pixel_values.astype(np.float16)

                return pixel_values, image_hash, image.size, image_grid_thws
        except Exception:
            logger.error("Exception in TokenizerManager:\n" +
                         get_exception_traceback())

    async def _process_single_image(self, image_data: Union[bytes, str]):
        if self.executor is not None:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self.executor,
                Qwen2VLImageProcessor._process_single_image_task,
                image_data,
            )
        else:
            return self._process_single_image_task(image_data)

    async def process_images_async(
        self,
        image_data: List[Union[str, bytes]],
        input_text,
        request_obj,
        *args,
        **kwargs,
    ):
        if not image_data:
            return None

        if isinstance(image_data, list) and len(image_data) > 0:
            # Multiple images
            if len(image_data) > 1:
                pixel_values, image_hashes, image_sizes, image_grid_thws = (
                    [],
                    [],
                    [],
                    [],
                )
                res = []
                for img_data in image_data:
                    res.append(self._process_single_image(img_data))
                res = await asyncio.gather(*res)
                for pixel_v, image_h, image_s, image_thw in res:
                    pixel_values.append(pixel_v)
                    image_hashes.append(image_h)
                    image_sizes.append(image_s)
                    image_grid_thws.append(image_thw)

                if isinstance(pixel_values[0], np.ndarray):
                    pixel_values = np.concatenate(pixel_values, axis=0)
            else:
                # A single image
                pixel_values, image_hash, image_size, image_grid_thw = (
                    await self._process_single_image(image_data[0])
                )
                image_hashes = [image_hash]
                image_sizes = [image_size]
                image_grid_thws = [image_grid_thw]
        elif isinstance(image_data, str):
            # A single image
            pixel_values, image_hash, image_size, image_grid_thw = (
                await self._process_single_image(image_data)
            )
            image_hashes = [image_hash]
            image_sizes = [image_size]
            image_grid_thws = [image_grid_thw]
        else:
            raise ValueError(f"Invalid image data: {image_data}")

        return {
            "pixel_values": pixel_values,
            "image_hashes": image_hashes,
            "image_sizes": image_sizes,
            "modalities": request_obj.modalities or ["image"],
            "image_grid_thws": image_grid_thws,
        }


class DeepseekVL2ImageProcessor(BaseImageProcessor):
    def __init__(self, hf_config, server_args, _processor):
        super().__init__(hf_config, server_args, _processor)

    @staticmethod
    def _process_single_image_task(image, input_text):
        return global_processor.__call__(conversations=input_text, images=image)

    async def _process_single_image(self, image_data: Union[bytes, str], input_text):
        if self.executor is not None:
            loop = asyncio.get_event_loop()
            image_inputs = await loop.run_in_executor(
                self.executor,
                DeepseekVL2ImageProcessor._process_single_image_task,
                image_data,
                input_text,
            )
        else:
            image_inputs = self._process_single_image_task(
                image_data, input_text)

        return image_inputs

    async def process_images_async(self, image_data, input_text, request_obj):
        if not image_data:
            return None

        if not isinstance(image_data, list):
            image_data = [image_data]

        images, image_hashes, image_sizes = [], [], []

        for image in image_data:
            pil_image, _size = load_image(image)
            pil_image = pil_image.convert("RGB")
            images += [pil_image]
            image_hashes += [hash(image)]
            image_sizes += [_size]
        res = await self._process_single_image(images, input_text)
        pixel_values = res["images"]
        input_ids = res["input_ids"]
        images_seq_mask = res["image_seq_mask"]
        images_spatial_crop = res["images_spatial_crop"]
        batched_images_spatial_crop = []
        batched_images_spatial_crop.append(images_spatial_crop)
        batched_images_spatial_crop = torch.stack(
            batched_images_spatial_crop, dim=0)

        return {
            "input_ids": input_ids.tolist(),
            "pixel_values": pixel_values,
            "image_hashes": image_hashes,
            "image_sizes": image_sizes,
            "image_seq_mask": images_seq_mask,
            "images_spatial_crop": batched_images_spatial_crop,
            "modalities": request_obj.modalities or ["image"],
        }


def get_image_processor(
    hf_config, server_args: ServerArgs, processor
) -> BaseImageProcessor:
    if "MllamaForConditionalGeneration" in hf_config.architectures:
        return MllamaImageProcessor(hf_config, server_args, processor)
    elif "Qwen2VLForConditionalGeneration" in hf_config.architectures:
        return Qwen2VLImageProcessor(hf_config, server_args, processor.image_processor)
    elif "MiniCPMV" in hf_config.architectures:
        return MiniCPMVImageProcessor(hf_config, server_args, processor)
    elif "DeepseekVL2ForCausalLM" in hf_config.architectures:
        return DeepseekVL2ImageProcessor(hf_config, server_args, processor)
    else:
        return LlavaImageProcessor(hf_config, server_args, processor.image_processor)


def get_dummy_image_processor():
    return DummyImageProcessor()
