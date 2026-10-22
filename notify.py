"""PushPlus HTTP acceptance is distinct from asynchronous channel delivery."""
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

LOG = logging.getLogger(__name__)


class PushError(RuntimeError):
    pass


class DeliveryUnknown(PushError):
    pass


@dataclass(frozen=True)
class Receipt:
    status: str
    duplicate: bool = False


class PushPlus:
    def __init__(self, token, access_key=None, session=None, sleep=time.sleep):
        if not token:
            raise PushError("PUSH_TOKEN 未配置，未发送日报")
        self.token = token
        self.access_key = access_key
        self.session = session or requests.Session()
        self.sleep = sleep

    def poll(self, short_code):
        if not self.access_key:
            return "accepted_unverified"
        for attempt in range(3):
            try:
                r = self.session.get("https://www.pushplus.plus/api/open/message/sendMessageResult", params={"shortCode": short_code}, headers={"access-key": self.access_key}, timeout=(5, 15))
                if r.status_code != 200:
                    return "accepted_unverified"
                body = r.json()
                if body.get("code") != 200:
                    return "accepted_unverified"
                status = body.get("data", {}).get("status")
                if status == 2:
                    return "delivered"
                if status == 3:
                    return "failed"
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                return "accepted_unverified"
            if attempt < 2:
                self.sleep(2 ** attempt)
        return "accepted_pending"

    def publish(self, key, title, content, expires_at, store):
        if expires_at <= datetime.now(timezone.utc):
            raise PushError("消息已超过有效交易窗口，未发送")
        with store:
            state = store.load()
            entry = state["entries"].get(key)
            if entry and entry["status"] == "delivered":
                return Receipt("delivered", duplicate=True)
            if entry and entry["status"] in ("accepted", "accepted_unverified", "accepted_pending"):
                status = self.poll(entry["short_code"])
                entry["status"] = status
                store.save(state)
                if status == "failed":
                    raise PushError("PushPlus确认上次投递失败；下次有限补跑可重试")
                return Receipt(status, duplicate=True)
            if entry and entry["status"] in ("preparing", "unknown"):
                raise DeliveryUnknown("上次发送结果未知，已停止自动重发；需核对PushPlus记录以免重复")
            attempts = entry.get("attempts", 0) if entry else 0
            if attempts >= 3:
                raise PushError("当日已达到3次确认失败后的尝试上限")
            entry = {"status": "preparing", "attempts": attempts + 1, "created_at": datetime.now(timezone.utc).isoformat(), "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            state["entries"][key] = entry
            # Bounded nonsecret ledger: keep the most recently written 120 notification keys.
            if len(state["entries"]) > 120:
                state["entries"] = dict(list(state["entries"].items())[-120:])
            store.save(state)  # Must succeed BEFORE the outbound POST.

            def fail(status, message):
                entry["status"] = status
                store.save(state)
                if status == "unknown":
                    raise DeliveryUnknown(message)
                raise PushError(message)

            try:
                r = self.session.post("https://www.pushplus.plus/send", json={"token": self.token, "title": title, "content": content, "template": "html", "channel": "wechat", "timestamp": int(expires_at.timestamp() * 1000)}, timeout=(5, 20))
            except requests.RequestException:
                fail("unknown", "PushPlus请求异常，结果未知；未自动重发")
            if r.status_code != 200:
                definite_rejection = r.status_code in (400, 401, 403, 413, 429)
                fail("failed" if definite_rejection else "unknown", f"PushPlus HTTP {r.status_code}，未认定投递成功")
            try:
                body = r.json()
                code = body.get("code")
            except (ValueError, AttributeError):
                fail("unknown", "PushPlus响应不是有效业务JSON，结果未知")
            if code != 200:
                safe_code = code if isinstance(code, int) else "invalid"
                fail("failed", f"PushPlus业务拒绝 code={safe_code}，未发送成功")
            short_code = body.get("data")
            if not isinstance(short_code, str) or not short_code.strip():
                fail("unknown", "PushPlus已受理但没有返回消息流水号，停止自动重发")
            entry.update(status="accepted", short_code=short_code)
            store.save(state)
            status = self.poll(short_code)
            entry["status"] = status
            store.save(state)
            if status == "failed":
                raise PushError("PushPlus已确认最终投递失败")
            LOG.info("PushPlus result=%s (never equate HTTP acceptance with delivery)", status)
            return Receipt(status)
