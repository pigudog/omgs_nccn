import os
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

from .logger import DBLogger


class _LocalChatResponse:
    def __init__(self, *, content: str, model: str):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = None
        self._model = model

    def model_dump(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "choices": [
                {
                    "message": {
                        "content": self.choices[0].message.content,
                    }
                }
            ],
            "usage": None,
        }


class OpenAIWrapper:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        db_path: str = "omgs_nccn_api_trace.db",
        provider: str = "azure",
    ):
        self.provider = provider
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.logger = DBLogger(db_path)
        self.verbose = os.getenv("OMGS_NCCN_LLM_VERBOSE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def chat_completion(
        self,
        *,
        model: str = "gpt-5-mini",
        messages=None,
        extra_body=None,
        **kwargs,
    ):
        if self.verbose:
            print(f"Calling model: {model} (provider: {self.provider})")
        start = time.time()

        if "max_tokens" in kwargs and "max_completion_tokens" not in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

        if self.provider == "openrouter" and extra_body:
            kwargs["extra_body"] = extra_body

        raw_request = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        if extra_body:
            raw_request["extra_body"] = extra_body

        resp = self.client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs,
        )

        latency_ms = (time.time() - start) * 1000
        output_text = resp.choices[0].message.content

        reasoning_details = None
        if hasattr(resp.choices[0].message, "reasoning_details"):
            reasoning_details = getattr(
                resp.choices[0].message, "reasoning_details", None
            )

        usage = getattr(resp, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "completion_tokens", None)
            total_tokens = getattr(usage, "total_tokens", None)
        else:
            input_tokens = output_tokens = total_tokens = None

        self.logger.log(
            timestamp=str(datetime.now()),
            model=model,
            temperature=kwargs.get("temperature"),
            input_text=str(messages),
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            raw_request=raw_request,
            raw_response=resp.model_dump(),
            latency_ms=latency_ms,
            extra_body=extra_body,
            reasoning_details=reasoning_details,
        )

        return resp


class LocalTransformersWrapper:
    def __init__(
        self,
        model_path: str,
        *,
        db_path: str = "omgs_nccn_api_trace.db",
        provider: str = "qwen-2.5-3b",
    ):
        self.provider = provider
        self.model_path = model_path
        self.logger = DBLogger(db_path)
        self.verbose = os.getenv("OMGS_NCCN_LLM_VERBOSE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._tokenizer = None
        self._model = None
        self._device = "cuda" if self._torch_cuda_available() else "cpu"

    def _torch_cuda_available(self) -> bool:
        import torch

        return bool(torch.cuda.is_available())

    def _ensure_loaded(self) -> None:
        if self._tokenizer is not None and self._model is not None:
            return
        from transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer
        import torch

        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path, use_fast=False)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            torch_dtype=torch.float16 if self._device == "cuda" else torch.float32,
        )
        if self._device == "cuda":
            self._model = self._model.to("cuda")
        if hasattr(self._model, "generation_config"):
            for key in ("temperature", "top_p", "top_k"):
                if hasattr(self._model.generation_config, key):
                    setattr(self._model.generation_config, key, None)
        self._model.eval()

    def chat_completion(
        self,
        *,
        model: str = "qwen-2.5-3b",
        messages=None,
        extra_body=None,
        **kwargs,
    ):
        del extra_body
        self._ensure_loaded()
        import torch

        if self.verbose:
            print(f"Calling local model: {model} (provider: {self.provider})")
        start = time.time()
        max_completion_tokens = int(
            kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 512
        )
        text = self._tokenizer.apply_chat_template(
            messages or [],
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self._tokenizer(text, return_tensors="pt")
        if self._device == "cuda":
            inputs = {k: v.to("cuda") for k, v in inputs.items()}
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=max_completion_tokens,
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        output_text = self._tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        ).strip()
        resp = _LocalChatResponse(content=output_text, model=model)

        latency_ms = (time.time() - start) * 1000
        raw_request = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        self.logger.log(
            timestamp=str(datetime.now()),
            model=model,
            temperature=kwargs.get("temperature"),
            input_text=str(messages),
            output_text=output_text,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            raw_request=raw_request,
            raw_response=resp.model_dump(),
            latency_ms=latency_ms,
            extra_body=None,
            reasoning_details=None,
        )
        return resp
