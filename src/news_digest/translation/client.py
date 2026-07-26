"""OpenAI 兼容翻译客户端；不绑定具体部署位置，一切来自配置。

使用流式响应（SSE）：长文生成可达数分钟，非流式会被反向代理的
读超时（如 Nginx 默认 60s）切断为 504；流式下数据持续到达，不触发网关超时。
"""

import json

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
        # 不发 temperature：推理系模型（如 o 系列）会拒绝该参数，各模型默认值即可
        payload = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,  # Anthropic 兼容后端必填
            "stream": True,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(article)},
            ],
        }
        parts: list[str] = []
        try:
            with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    detail = response.read()[:160].decode("utf-8", "replace").replace("\n", " ")
                    raise TranslationError(
                        f"HTTP {response.status_code}（{article.slug}）{detail}"
                    )
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        choices = json.loads(data).get("choices") or []
                        delta = choices[0].get("delta", {}).get("content") if choices else None
                    except (ValueError, AttributeError, IndexError):
                        continue  # 跳过 usage/keep-alive 等非内容块
                    if delta:
                        parts.append(delta)
        except httpx.HTTPError as error:
            raise TranslationError(
                f"请求失败：{error.__class__.__name__}（{article.slug}）"
            ) from error
        content = "".join(parts)
        if not content.strip():
            raise TranslationError(f"流式响应无内容（{article.slug}）")
        return content

    def close(self) -> None:
        self._client.close()
