"""OpenAI 兼容翻译客户端；不绑定具体部署位置，一切来自配置。"""

import httpx

from news_digest.config import TranslationConfig
from news_digest.models import Article
from news_digest.translation.schema import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt


class TranslationError(RuntimeError):
    """接口调用失败（网络、鉴权、限流等）。信息中不得包含凭据。"""


class ApiTranslator:
    """POST {base_url}/chat/completions，OpenAI Chat Completions 兼容。"""

    def __init__(
        self, config: TranslationConfig, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        missing = [
            name
            for name, value in (
                ("TRANSLATION_API_BASE_URL", config.base_url),
                ("TRANSLATION_API_KEY", config.api_key),
                ("TRANSLATION_MODEL", config.model),
            )
            if not value
        ]
        if missing:
            raise TranslationError(f"翻译接口配置缺失：{', '.join(missing)}（写入 .env.local）")
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
        )

    @property
    def label(self) -> str:
        return f"{self._config.model}@{PROMPT_VERSION}"

    @property
    def model(self) -> str:
        return self._config.model

    def translate(self, article: Article) -> str:
        """返回模型原始文本输出；解析与校验由 schema 层负责。"""
        payload = {
            "model": self._config.model,
            "temperature": 0.3,
            "max_tokens": self._config.max_tokens,  # Anthropic 兼容后端必填
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(article)},
            ],
        }
        try:
            response = self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as error:
            raise TranslationError(
                f"请求失败：{error.__class__.__name__}（{article.slug}）"
            ) from error
        if response.status_code != 200:
            detail = response.text[:160].replace("\n", " ")
            raise TranslationError(f"HTTP {response.status_code}（{article.slug}）{detail}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as error:
            raise TranslationError(f"响应结构异常（{article.slug}）") from error
        if not isinstance(content, str) or not content.strip():
            raise TranslationError(f"响应内容为空（{article.slug}）")
        return content

    def close(self) -> None:
        self._client.close()
