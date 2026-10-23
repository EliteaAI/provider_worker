"""llm_settings must carry the session-auth headers for every provider.

Session-auth runtimes put only a placeholder in api_key ("session") and
authenticate with the user's session cookie in default_headers. Plugins
(e.g. deepwiki artifact transport) reuse llm_settings to call the platform,
so the headers must survive for OpenAI-compatible models, not only Anthropic.

Reuses the `_load_provider_tools` harness from test_5854_dependency_contract.py.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_5854_dependency_contract import _load_provider_tools  # noqa: E402


class ChatOpenAI:  # name drives provider detection
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class ChatAnthropic(ChatOpenAI):
    pass


def _extract(provider_tools, llm):
    toolkit = provider_tools.Toolkit.__new__(provider_tools.Toolkit)
    toolkit.llm = llm
    toolkit.elitea = types.SimpleNamespace(project_id=96)
    return toolkit._extract_llm_settings()


@pytest.fixture
def provider_tools(monkeypatch):
    return _load_provider_tools(monkeypatch)


def test_openai_settings_keep_session_cookie(provider_tools):
    settings = _extract(provider_tools, ChatOpenAI(
        model_name="gpt-5.2",
        openai_api_key="session",
        openai_api_base="http://main:8080/llm/v1",
        openai_organization="96",
        default_headers={"Cookie": "elitea_session=abc"},
    ))

    assert settings["provider"] == "openai"
    assert settings["api_key"] == "session"
    assert settings["default_headers"] == {"Cookie": "elitea_session=abc"}
    assert settings["organization"] == "96"


def test_openai_settings_without_headers_omit_them(provider_tools):
    settings = _extract(provider_tools, ChatOpenAI(
        model_name="gpt-5.2",
        openai_api_key="token",
        openai_api_base="http://main:8080/llm/v1",
        default_headers=None,
    ))

    assert "default_headers" not in settings


def test_anthropic_settings_still_keep_headers(provider_tools):
    settings = _extract(provider_tools, ChatAnthropic(
        model="claude",
        anthropic_api_key="session",
        anthropic_api_url="http://main:8080/llm",
        default_headers={"openai-organization": "96", "Cookie": "elitea_session=abc"},
    ))

    assert settings["default_headers"]["Cookie"] == "elitea_session=abc"
    assert settings["organization"] == "96"


class AutoChatModel:
    _llm_type = "elitea-auto"

    def __init__(self, owner):
        self.owner = owner


def test_auto_model_settings_come_from_the_platform_client(provider_tools):
    owner = types.SimpleNamespace(
        base_url="http://main:8080", llm_path="/llm/v1", project_id=96, auth_token=None,
        _llm_api_key="session", _llm_cookie_headers={"Cookie": "elitea_session=abc"},
        api_extra_headers=None)
    toolkit = provider_tools.Toolkit.__new__(provider_tools.Toolkit)
    toolkit.llm = AutoChatModel(owner)
    toolkit.elitea = types.SimpleNamespace(project_id=96, model_image_generation="gpt-image-1")
    settings = toolkit._extract_llm_settings()

    assert settings["api_base"] == "http://main:8080/llm/v1"
    assert settings["api_key"] == "session"
    assert settings["default_headers"] == {"Cookie": "elitea_session=abc"}
    assert settings["organization"] == "96" and settings["project_id"] == 96
    assert settings["model_image_generation"] == "gpt-image-1"
