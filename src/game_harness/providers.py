"""Real providers. Game actions can only come from the typed decision endpoint."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


class ProviderError(RuntimeError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    decision_url: str
    decision_model: str
    decision_timeout: float
    coach_base_url: str
    coach_model: str
    coach_api_key: str
    coach_timeout: float

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env", override=False)
        if os.getenv("DECISION_THINK_MODE", "never") != "never":
            raise ProviderError("首版只允许 DECISION_THINK_MODE=never")
        return cls(
            os.getenv("DECISION_URL", "http://127.0.0.1:18090/v1/systemone"),
            os.getenv("DECISION_MODEL", "startlux-decision-27b-autothink"),
            float(os.getenv("DECISION_TIMEOUT", "30")),
            os.getenv("COACH_BASE_URL", "https://api.startluxai.com/v1").rstrip("/"),
            os.getenv("COACH_MODEL", "glm-5.3"),
            os.getenv("COACH_API_KEY", ""),
            float(os.getenv("COACH_TIMEOUT", "180")),
        )


def extract_json(text: str) -> dict:
    """Decode a single JSON object, tolerating Markdown fences but never executing it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ProviderError("教练没有返回 JSON 对象") from None
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            raise ProviderError("教练返回的 JSON 无法解析") from None
    if not isinstance(value, dict):
        raise ProviderError("教练输出必须为 JSON 对象")
    return value


def validate_candidate(candidate: dict) -> dict[str, str]:
    expected = {"decision_instructions", "strategy_notes"}
    if not isinstance(candidate, dict) or set(candidate) != expected:
        raise ProviderError("Harness 只能包含 decision_instructions 与 strategy_notes")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 12000 for v in candidate.values()):
        raise ProviderError("Harness 两个文本字段必须非空且不超过 12000 字符")
    return {k: candidate[k].strip() for k in sorted(expected)}


class DecisionProvider:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.decision_timeout, trust_env=False)

    def health(self) -> dict:
        base = self.settings.decision_url.split("/v1/", 1)[0]
        try:
            response = self.client.get(base + "/health", timeout=8)
            response.raise_for_status()
            data = response.json()
            model = data.get("model")
            if self.settings.decision_model and model and model != self.settings.decision_model:
                raise ProviderError("Decision 服务模型与配置的固定模型不一致")
            return {"ok": True, "model": model, "request_think_mode": "never",
                    "server_default_think_mode": data.get("think_mode"),
                    "identity_verification": "service_reported_name"}
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("Decision 健康检查失败，请检查本地服务") from None

    def decide(self, state, instructions, criteria, request_id) -> dict:
        choices = {item["id"]: item["label"] for item in criteria}
        if len(choices) != len(criteria) or not choices:
            raise ProviderError("动作目录为空或 ID 重复")
        payload = {"model": self.settings.decision_model,
                   "state": json.dumps(state, ensure_ascii=False),
                   "questions": {"primary_action": {"type": "choice",
                                 "instructions": instructions, "criteria": choices,
                                 "allow_none": False}},
                   "think_mode": "never", "return_details": True,
                   "allow_none": False}
        start = time.perf_counter()
        try:
            response = self.client.post(self.settings.decision_url, json=payload)
            if response.status_code != 200:
                raise ProviderError(f"Decision HTTP {response.status_code}；没有执行动作")
            data = response.json()
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError):
            raise ProviderError("Decision 请求失败或返回不是 JSON；没有执行动作") from None
        elapsed = (time.perf_counter() - start) * 1000
        try:
            answer = data["answers"]["primary_action"]
            choice = answer["choice"]
            if choice is None:
                raise ProviderError("Decision 返回空动作（弃选）；没有执行动作，也不会本地代选")
            probs = answer["probabilities"]
            confidence = answer["confidence"]
            values = [confidence, *probs.values()]
            valid = (choice in choices and set(probs) == set(choices)
                     and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in values)
                     and abs(sum(probs.values()) - 1) <= 0.02)
            if not valid:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("Decision 返回动作/概率无效；没有执行动作") from None
        model = data.get("model")
        if not isinstance(model, str) or (self.settings.decision_model and model != self.settings.decision_model):
            raise ProviderError("Decision 返回的模型身份不匹配；没有执行动作")
        thought = answer.get("details", {}).get("thought", {})
        used, tokens = thought.get("used"), thought.get("tokens")
        if used not in (None, False) or (tokens is not None and tokens != 0):
            raise ProviderError("Decision 未遵守关闭思考设置；没有执行动作")
        return {"request_id": request_id, "observation_revision": state.get("step"),
                "choice_id": choice, "candidate_hash": digest(criteria),
                "model": model, "elapsed_ms": round(elapsed, 3),
                "think_mode": "never", "think_used": used, "reasoning_tokens": tokens,
                "thinking_verification": "verified" if used is False and tokens == 0 else "not_reported",
                "raw_response": data, "response_hash": digest(data)}

    def close(self):
        self.client.close()


class CoachProvider:
    """Only instantiated by initialization/evolution, never by the game runner."""
    def __init__(self, settings: Settings, on_call: Callable | None = None,
                 client: httpx.Client | None = None):
        self.settings = settings
        self.on_call = on_call
        self.client = client or httpx.Client(timeout=settings.coach_timeout,
                                            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        self.calls: list[dict] = []

    def complete(self, messages, purpose="reflection") -> str:
        if not self.settings.coach_api_key:
            raise ProviderError("未配置 COACH_API_KEY；请在本地 .env 中设置")
        payload = {"model": self.settings.coach_model, "messages": messages,
                   "max_tokens": 4096, "temperature": 0.5,
                   "response_format": {"type": "json_object"},
                   "thinking": {"type": "disabled"}}
        start = time.perf_counter()
        try:
            response = self.client.post(self.settings.coach_base_url + "/chat/completions",
                                       headers={"Authorization": "Bearer " + self.settings.coach_api_key},
                                       json=payload)
            if response.status_code != 200:
                raise ProviderError(f"教练 API HTTP {response.status_code}")
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise ProviderError("教练未返回可用正文")
            if choice.get("finish_reason") == "length":
                raise ProviderError("教练输出达到长度上限，未采纳不完整内容")
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("教练请求失败或响应格式无效") from None
        info = {"purpose": purpose, "requested_model": self.settings.coach_model,
                "returned_model": data.get("model"), "usage": data.get("usage", {}),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "response_hash": digest(content)}
        self.calls.append(info)
        if self.on_call:
            self.on_call(info)
        return content

    def initialize(self, rules: str | None = None, size: int | None = None,
                   max_steps: int | None = None, *, game_context: dict | None = None) -> tuple[dict, dict]:
        context = deepcopy(game_context) if game_context is not None else {
            "rules": rules, "config": {"board_size": size, "max_steps": max_steps}}
        if not isinstance(context, dict) or not isinstance(context.get("rules"), str):
            raise ProviderError("初始化必须提供游戏适配器的固定规则")
        try:
            context_text = json.dumps(context, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ProviderError("游戏资料必须为可序列化的 JSON 数据") from None
        prompt = ("根据下方游戏适配器资料，生成第一版游戏资料和候选玩家 Harness。所有局内决策由 StartLux Decision 完成，"
                  "你只在开局前和局后工作。不能生成可执行代码或改变规则。"
                  "返回一个 JSON 对象，仅含 brief 和 candidate。brief 包含 background、goal、"
                  "rules、precautions、strategy 字段（中文文字或列表）；candidate 仅含两个非空字符串："
                  "decision_instructions（详细但简洁的选择规则）、strategy_notes（策略建议）。"
                  "根据可见状态与完整动作目录指导模型，不要求外部工具。"
                  "尊重 score_spec 的计分方向与预算；若包含 opponent_policy，该对手策略固定，不得优化或覆盖。"
                  "候选模型将被告知当前 actor 和 policy_role，策略须适用于双方身份。"
                  "不要把某个种子、隐藏状态、固定路径或测试答案写进策略。"
                  "游戏规则、计分、预算与对手资料为固定上下文：\n"
                  + context_text + "\n严格按以下结构输出，不增加字段："
                  '{"brief":{"background":"...","goal":"...","rules":"...",'
                  '"precautions":"...","strategy":"..."},"candidate":{'
                  '"decision_instructions":"...","strategy_notes":"..."}}')
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(2):
            content = self.complete(messages, purpose="initialization")
            try:
                output = extract_json(content)
                candidate = validate_candidate(output.get("candidate"))
                brief = output.get("brief")
                required = {"background", "goal", "rules", "precautions", "strategy"}
                if not isinstance(brief, dict) or not required <= set(brief):
                    raise ProviderError("初始化资料缺少背景、目标、规则、注意事项或攻略")
                return brief, candidate
            except ProviderError as exc:
                if attempt:
                    raise
                messages.extend([{"role": "assistant", "content": content},
                                 {"role": "user", "content": f"结构校验失败：{exc}。请修复后仅返回完整 JSON。"}])
        raise ProviderError("初始化失败")

    def close(self):
        self.client.close()
