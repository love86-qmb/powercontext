"""零外部依赖的本地嵌入函数。

为什么不用现成的：pyseekdb 的默认嵌入函数是 ONNX 句子模型，首次使用要联网下载权重，
评审在自己环境里复现时很容易卡在这一步。这里用一个确定性的字符 n-gram 哈希向量替代：

* 中文按「单字 + 相邻二元组」、英文/数字按词切分；
* 每个 token 用 blake2b 散列到固定维度，带正负号（hashing trick）；
* 结果做 L2 归一化，余弦距离即可直接用于 seekdb 的向量检索。

对「短句记忆」这种场景，字符二元组的重合度本来就足以区分「赊账」「砸杯子」和「带鱼」，
而且完全离线、可重复、跨平台一致。想换成真实模型见 README 的 `EMBEDDING_MODE` 说明。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from pyseekdb.client.embedding_function import register_embedding_function

DIM = 256
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokens(text: str) -> list[str]:
    text = (text or "").lower()
    out = _WORD.findall(text)
    chars = _CJK.findall(text)
    out.extend(chars)
    out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


@register_embedding_function
class HashEmbedding:
    """实现 pyseekdb 的 EmbeddingFunction 协议（Chroma 风格）。

    ⚠️ 必须注册进 pyseekdb 的注册表：集合会把嵌入函数的 name + config 持久化，
    下次打开集合时按 name 反查类，查不到就报 "data may be corrupted"（踩过这个坑）。
    注册是装饰器做的，app.py 只要 import 本模块就已生效。

    另外 pyseekdb 在多个位置直接读 `embedding_function.dimension` 并要求它是 int，
    所以这里用类属性而不是方法（`dimension_of()` 两种写法都兼容，但别处不兼容）。
    """

    dimension = DIM

    @staticmethod
    def name() -> str:
        return "tavern-hash-ngram"

    def get_config(self) -> dict[str, Any]:
        """返回可复现的配置（不含 name），让 pyseekdb 能记录/还原它。"""
        return {"dim": DIM, "granularity": "cjk-unigram+bigram+words", "version": 1}

    @staticmethod
    def build_from_config(config: dict[str, Any] | None = None) -> "HashEmbedding":
        return HashEmbedding()

    def __call__(self, documents: Any) -> list[list[float]]:
        single = isinstance(documents, str)
        docs = [documents] if single else list(documents or [])
        return [self._embed(str(doc)) for doc in docs]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * DIM
        for token in tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            weight = 1.0 + digest[5] / 255.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)
