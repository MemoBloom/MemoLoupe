"""connect.registry 单元测试：provider 注册表。"""

from __future__ import annotations

import pytest

from memoloupe.connect.registry import PROVIDERS, ProviderSpec, get_provider_spec
from memoloupe.connect.store import ConnectionStoreError


class TestProviders:
    def test_exactly_qwen_and_mimo(self) -> None:
        assert set(PROVIDERS) == {"qwen", "mimo"}

    def test_specs_complete(self) -> None:
        for provider_id, spec in PROVIDERS.items():
            assert isinstance(spec, ProviderSpec)
            assert spec.provider_id == provider_id
            assert spec.label
            assert spec.default_base_url.startswith("https://")
            assert spec.default_media_model
            assert spec.default_text_model
            assert set(spec.capabilities) == {"mediaUnderstanding", "text", "asr"}
            assert all(isinstance(v, bool) for v in spec.capabilities.values())
            assert spec.health_check_path == "/models"
            # asr 能力与 transport 必须一致：有 ASR 能力的 provider 必须声明 transport。
            if spec.capabilities["asr"]:
                assert spec.default_asr_model
                assert spec.asr_transport
            else:
                assert spec.default_asr_model is None
                assert spec.asr_transport is None

    def test_qwen_defaults(self) -> None:
        spec = PROVIDERS["qwen"]
        assert spec.default_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert spec.default_media_model == "qwen3.8-flash"
        assert spec.default_text_model == "qwen3.8-flash"
        # Qwen ASR（qwen3-asr-flash）走 OpenAI 兼容 chat/completions + input_audio。
        assert spec.default_asr_model == "qwen3-asr-flash"
        assert spec.asr_transport == "qwen-chat"
        assert spec.capabilities == {
            "mediaUnderstanding": True,
            "text": True,
            "asr": True,
        }

    def test_mimo_defaults(self) -> None:
        spec = PROVIDERS["mimo"]
        assert spec.default_base_url == "https://api.xiaomimimo.com/v1"
        assert spec.default_media_model == "mimo-v2.5"
        assert spec.default_text_model == "mimo-v2.5"
        # D-057：MiMo ASR（mimo-v2.5-asr）走 chat/completions + input_audio。
        assert spec.default_asr_model == "mimo-v2.5-asr"
        assert spec.asr_transport == "mimo-chat"
        assert spec.capabilities == {
            "mediaUnderstanding": True,
            "text": True,
            "asr": True,
        }


class TestGetProviderSpec:
    def test_known_id(self) -> None:
        assert get_provider_spec("qwen") is PROVIDERS["qwen"]

    def test_unknown_id_raises_with_supported_list(self) -> None:
        with pytest.raises(ConnectionStoreError) as exc_info:
            get_provider_spec("openai-compatible")
        message = str(exc_info.value)
        assert "openai-compatible" in message
        assert "qwen" in message
        assert "mimo" in message
