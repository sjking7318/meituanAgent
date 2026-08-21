"""Live end-to-end smoke check against a running server (M3 verification).

Exercises: ingest a document, then the three supervisor routes:
knowledge_qa (must carry citations), chitchat, and clarify.

Run the server first (`uv run sales-assistant`), then:
    uv run python scripts/e2e_check.py
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import httpx

BASE = "http://localhost:8000"
TENANT = str(uuid.uuid4())
USER = str(uuid.uuid4())
HEADERS = {"X-Tenant-ID": TENANT, "X-User-ID": USER}

DOC = """# 新商家佣金政策

## 首月免佣
新入驻的商家在开通后的首个自然月内免收佣金，第二个月起按 5% 收取。

## 结算周期
佣金按 T+7 结算，节假日顺延。
"""


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


def _ask(client: httpx.Client, conv_id: str, content: str) -> dict[str, Any]:
    resp = client.post(
        f"{BASE}/v1/conversations/{conv_id}/messages",
        json={"content": content, "stream": False},
        headers={**HEADERS, **_idem()},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    with httpx.Client() as client:
        # 1. Ingest a knowledge document.
        kb = client.post(
            f"{BASE}/v1/knowledge/knowledge-bases",
            json={"name": "e2e-kb"},
            headers=HEADERS,
            timeout=30.0,
        )
        kb.raise_for_status()
        kb_id = kb.json()["id"]
        print(f"[ingest] knowledge_base={kb_id}")

        ing = client.post(
            f"{BASE}/v1/knowledge/knowledge-bases/{kb_id}/documents",
            json={"title": "佣金政策", "content": DOC, "acl_tokens": [TENANT]},
            headers=HEADERS,
            timeout=120.0,
        )
        ing.raise_for_status()
        print(f"[ingest] status={ing.json()['status']} chunks={ing.json()['chunk_count']}")
        version_id = ing.json()["document_version_id"]

        ok = True

        # 1b. Knowledge governance: list the stored chunks of this version.
        chunks_resp = client.get(
            f"{BASE}/v1/knowledge/document-versions/{version_id}/chunks",
            headers=HEADERS,
            timeout=30.0,
        )
        chunks_resp.raise_for_status()
        chunks = chunks_resp.json()
        print(f"[chunks] count={len(chunks)}")
        if chunks:
            first = chunks[0]
            print(f"[chunks] first: id={first['chunk_id']} title={first['title']!r} "
                  f"section={first['section_path']!r}")
        if not chunks or not all(c["title"] for c in chunks):
            print("  !! expected every chunk to carry a title")
            ok = False

        # 2. Create a conversation.
        conv = client.post(
            f"{BASE}/v1/conversations",
            json={"title": "e2e"},
            headers=HEADERS,
            timeout=30.0,
        )
        conv.raise_for_status()
        conv_id = conv.json()["id"]
        print(f"[conv] id={conv_id}")

        # 3a. knowledge_qa route -> must produce citations.
        qa = _ask(client, conv_id, "新商家首月的佣金政策是什么？")
        msg = qa["assistant_message"]
        print(f"\n[knowledge_qa] answer={msg['content'][:120]!r}")
        print(f"[knowledge_qa] citations={msg['citations']}")
        if not msg["citations"]:
            print("  !! expected citations for a grounded answer")
            ok = False

        # 3b. chitchat route.
        chat = _ask(client, conv_id, "你好，在吗")
        cmsg = chat["assistant_message"]
        print(f"\n[chitchat] answer={cmsg['content'][:120]!r}")
        if cmsg["citations"]:
            print("  !! chitchat should carry no citations")
            ok = False

        # 3c. clarify route (ambiguous single char).
        clr = _ask(client, conv_id, "?")
        lmsg = clr["assistant_message"]
        print(f"\n[clarify] answer={lmsg['content'][:120]!r}")
        if "补充" not in lmsg["content"]:
            print("  !! expected a clarifying prompt")
            ok = False

        # 4. Multi-turn memory: state a fact, then ask about it in a later turn.
        mem_conv = client.post(
            f"{BASE}/v1/conversations",
            json={"title": "memory"},
            headers=HEADERS,
            timeout=30.0,
        )
        mem_conv.raise_for_status()
        mconv_id = mem_conv.json()["id"]
        _ask(client, mconv_id, "你好，我叫王伟，负责华东区的餐饮商家。")
        recall = _ask(client, mconv_id, "你还记得我负责哪个区域吗？")
        rmsg = recall["assistant_message"]["content"]
        print(f"\n[memory] answer={rmsg[:140]!r}")
        if "华东" not in rmsg:
            print("  !! model did not recall the earlier fact (华东)")
            ok = False

        print("\n=== RESULT:", "PASS ✅" if ok else "FAIL ❌", "===")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
