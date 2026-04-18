"""
v8 AI SI Platform — 텔레그램 봇
프로젝트 상태 확인, GATE 승인, 수정 요청을 텔레그램에서 처리.

사용법: python tools/telegram_bot.py
환경변수: TELEGRAM_BOT_TOKEN, V8_API_URL, V8_EMAIL, V8_PASSWORD
"""

import asyncio
import json
import logging
import os
import sys
from urllib.parse import quote

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("v8-telegram")

# 설정
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8422351975:AAE8SwL3dSY6hCR956bpRxqcuKl1gCJwkIA")
API_URL = os.environ.get("V8_API_URL", "http://localhost:8000")
V8_EMAIL = os.environ.get("V8_EMAIL", "admin@platform.local")
V8_PASSWORD = os.environ.get("V8_PASSWORD", "admin1234")
ALLOWED_USERS = {2122237674}  # 허용된 텔레그램 유저 ID

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 대화 히스토리 (chat_id별, 최근 20턴 유지)
_chat_history: dict[int, list[dict]] = {}
MAX_HISTORY = 20


def _add_history(chat_id, role, content):
    if chat_id not in _chat_history:
        _chat_history[chat_id] = []
    _chat_history[chat_id].append({"role": role, "content": content})
    # 최근 20턴만 유지
    if len(_chat_history[chat_id]) > MAX_HISTORY:
        _chat_history[chat_id] = _chat_history[chat_id][-MAX_HISTORY:]


def _get_history(chat_id) -> str:
    msgs = _chat_history.get(chat_id, [])
    if not msgs:
        return ""
    lines = []
    for m in msgs:
        prefix = "사용자" if m["role"] == "user" else "봇"
        lines.append(f"{prefix}: {m['content'][:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# v8 API 헬퍼
# ---------------------------------------------------------------------------

_token_cache = {"token": None}


async def _login(session):
    async with session.post(f"{API_URL}/api/v1/auth/login",
                            json={"email": V8_EMAIL, "password": V8_PASSWORD}) as r:
        d = await r.json()
        _token_cache["token"] = d.get("token", "")
    return _token_cache["token"]


async def _api(session, method, path, body=None):
    if not _token_cache["token"]:
        await _login(session)
    headers = {"Authorization": f"Bearer {_token_cache['token']}",
               "Content-Type": "application/json"}
    url = f"{API_URL}{path}"
    async with session.request(method, url, headers=headers, json=body) as r:
        if r.status == 401:
            await _login(session)
            headers["Authorization"] = f"Bearer {_token_cache['token']}"
            async with session.request(method, url, headers=headers, json=body) as r2:
                return r2.status, await r2.json() if r2.content_type == "application/json" else {}
        try:
            return r.status, await r.json()
        except Exception:
            return r.status, {}


# ---------------------------------------------------------------------------
# 명령어 처리
# ---------------------------------------------------------------------------

async def cmd_status(session, chat_id):
    """프로젝트 전체 현황"""
    _, engs = await _api(session, "GET", "/api/v1/engagements")
    if not engs.get("engagements"):
        return await send(session, chat_id, "📋 인게이지먼트가 없습니다.")

    for eng in engs["engagements"][:3]:
        eid = eng["id"]
        _, projs = await _api(session, "GET", f"/api/v1/engagements/{eid}/projects")

        msg = f"📋 *{eng['name']}*\n상태: `{eng['status']}`\n\n"

        if projs.get("projects"):
            for proj in projs["projects"]:
                pid = proj["id"]
                _, nodes_data = await _api(session, "GET", f"/api/v1/projects/{pid}/nodes")
                nodes = nodes_data.get("nodes", [])

                total = sum(1 for n in nodes if n["node_type"] != "GATE" and n["state"] != "SKIPPED")
                done = sum(1 for n in nodes if n["state"] == "COMPLETED" and n["node_type"] != "GATE")
                failed = sum(1 for n in nodes if n["state"] == "FAILED")
                progress = sum(1 for n in nodes if n["state"] == "IN_PROGRESS")
                pct = round(done / total * 100) if total else 0

                msg += f"*{proj['name']}*\n"
                msg += f"진행률: {done}/{total} ({pct}%)\n"
                if progress:
                    msg += f"🔄 실행중: {progress}개\n"
                if failed:
                    msg += f"❌ 실패: {failed}개\n"

                # GATE 상태
                gates = [n for n in nodes if n["node_type"] == "GATE" and n["state"] == "AWAITING_APPROVAL"]
                for g in gates:
                    msg += f"🔒 승인 대기: `{g['name']}`\n"
                    msg += f"   /approve\\_{g['id'][:8]}\n"

                msg += "\n"

        await send(session, chat_id, msg)


async def cmd_approve(session, chat_id, gate_id_prefix):
    """GATE 승인"""
    # gate_id 앞 8자로 검색
    _, engs = await _api(session, "GET", "/api/v1/engagements")
    for eng in engs.get("engagements", []):
        _, projs = await _api(session, "GET", f"/api/v1/engagements/{eng['id']}/projects")
        for proj in projs.get("projects", []):
            _, nodes_data = await _api(session, "GET", f"/api/v1/projects/{proj['id']}/nodes")
            for n in nodes_data.get("nodes", []):
                if n["id"].startswith(gate_id_prefix) and n["state"] == "AWAITING_APPROVAL":
                    status, result = await _api(session, "POST",
                                                f"/api/v1/projects/{proj['id']}/nodes/{n['id']}/approve")
                    if status == 200:
                        await send(session, chat_id, f"✅ 승인 완료: `{n['name']}`")
                    else:
                        await send(session, chat_id, f"❌ 승인 실패: {result.get('detail', status)}")
                    return

    await send(session, chat_id, "❌ 해당 GATE를 찾을 수 없습니다.")


async def cmd_revise(session, chat_id, text):
    """수정 요청 — "노드이름: 수정 내용" 형식"""
    if ":" not in text:
        await send(session, chat_id, "📝 형식: `/revise 산출물이름: 수정 내용`\n예: `/revise PRD: 카카오페이 결제 추가해주세요`")
        return

    node_name, request_text = text.split(":", 1)
    node_name = node_name.strip()
    request_text = request_text.strip()

    # 노드 검색
    _, engs = await _api(session, "GET", "/api/v1/engagements")
    for eng in engs.get("engagements", []):
        _, projs = await _api(session, "GET", f"/api/v1/engagements/{eng['id']}/projects")
        for proj in projs.get("projects", []):
            _, nodes_data = await _api(session, "GET", f"/api/v1/projects/{proj['id']}/nodes")
            for n in nodes_data.get("nodes", []):
                if node_name in n["name"] and n["node_type"] == "TASK" and n["state"] == "COMPLETED":
                    await send(session, chat_id, f"📝 수정 요청 전송: *{n['name']}*\nAI가 수정 중입니다...")
                    status, result = await _api(session, "POST",
                                                f"/api/v1/projects/{proj['id']}/nodes/{n['id']}/revise",
                                                {"request": request_text})
                    if status == 200:
                        msg = f"✅ 수정 완료 (v{result.get('version')})"
                        impact = result.get("impact", "")
                        if result.get("invalidated"):
                            msg += f"\n→ 하위 {result['invalidated']}개 노드 재실행"
                        if result.get("upstream_revised"):
                            msg += f"\n📝 상위 반영: {', '.join(result['upstream_revised'])}"
                        await send(session, chat_id, msg)
                    else:
                        await send(session, chat_id, f"❌ 실패: {result.get('detail', status)}")
                    return

    await send(session, chat_id, f"❌ `{node_name}` 산출물을 찾을 수 없습니다.")


async def cmd_failed(session, chat_id):
    """실패 노드 목록"""
    _, engs = await _api(session, "GET", "/api/v1/engagements")
    msg = "❌ *실패 노드*\n\n"
    found = False
    for eng in engs.get("engagements", []):
        _, projs = await _api(session, "GET", f"/api/v1/engagements/{eng['id']}/projects")
        for proj in projs.get("projects", []):
            _, nodes_data = await _api(session, "GET", f"/api/v1/projects/{proj['id']}/nodes")
            for n in nodes_data.get("nodes", []):
                if n["state"] == "FAILED":
                    found = True
                    msg += f"• `{n['name']}` (retry={n.get('retry_count', 0)})\n"
                    msg += f"  /retry\\_{n['id'][:8]}\n"

    if not found:
        msg += "없음 ✅"
    await send(session, chat_id, msg)


async def cmd_retry(session, chat_id, node_id_prefix):
    """노드 재시도"""
    _, engs = await _api(session, "GET", "/api/v1/engagements")
    for eng in engs.get("engagements", []):
        _, projs = await _api(session, "GET", f"/api/v1/engagements/{eng['id']}/projects")
        for proj in projs.get("projects", []):
            _, nodes_data = await _api(session, "GET", f"/api/v1/projects/{proj['id']}/nodes")
            for n in nodes_data.get("nodes", []):
                if n["id"].startswith(node_id_prefix):
                    status, result = await _api(session, "POST",
                                                f"/api/v1/projects/{proj['id']}/nodes/{n['id']}/retry")
                    if status == 200:
                        await send(session, chat_id, f"🔄 재시도: `{n['name']}`")
                    else:
                        await send(session, chat_id, f"❌ 실패: {result.get('detail', status)}")
                    return

    await send(session, chat_id, "❌ 노드를 찾을 수 없습니다.")


async def cmd_natural(session, chat_id, text):
    """자연어 대화 — Claude에게 프로젝트 컨텍스트와 함께 전달"""
    import shutil

    cli = shutil.which("claude")
    if not cli:
        await send(session, chat_id, "❌ CLI를 찾을 수 없습니다.")
        return

    _add_history(chat_id, "user", text)

    # 프로젝트 컨텍스트 수집
    _, engs = await _api(session, "GET", "/api/v1/engagements")
    context = ""
    project_id = ""
    engagement_id = ""
    for eng in engs.get("engagements", []):
        engagement_id = eng["id"]
        _, projs = await _api(session, "GET", f"/api/v1/engagements/{eng['id']}/projects")
        for proj in projs.get("projects", []):
            project_id = proj["id"]
            _, nd = await _api(session, "GET", f"/api/v1/projects/{proj['id']}/nodes")
            nodes = nd.get("nodes", [])
            total = sum(1 for n in nodes if n["node_type"] != "GATE" and n["state"] != "SKIPPED")
            done = sum(1 for n in nodes if n["state"] == "COMPLETED" and n["node_type"] != "GATE")
            failed = [n for n in nodes if n["state"] == "FAILED"]
            in_progress = [n for n in nodes if n["state"] == "IN_PROGRESS"]
            gates = [n for n in nodes if n["node_type"] == "GATE" and n["state"] == "AWAITING_APPROVAL"]
            completed = [n["name"] for n in nodes if n["state"] == "COMPLETED" and n["node_type"] == "TASK"]

            context = f"""프로젝트: {eng['name']} ({eng['status']})
진행률: {done}/{total} ({round(done/total*100) if total else 0}%)
실행중: {', '.join(n['name'] for n in in_progress) or '없음'}
실패: {', '.join(n['name'] for n in failed) or '없음'}
승인대기 GATE: {', '.join(n['name'] for n in gates) or '없음'}
완료 산출물: {', '.join(completed[:20])}"""
            break
        break

    # Claude에게 대화 전달
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        cli, "--print", "--model", "claude-sonnet-4-6",
        "--max-turns", "1", "--no-session-persistence",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    history = _get_history(chat_id)

    prompt = f"""너는 AI SI 제조 플랫폼의 프로젝트 매니저 봇이야. 텔레그램으로 대화 중이야.
짧고 친근하게 답해. 이모지 적절히 써.

현재 프로젝트 상태:
{context}

이전 대화:
{history}

가능한 작업:
- 프로젝트 상태 확인 → 답변 끝에 [ACTION:status] 태그 추가
- 산출물 수정 요청 → 답변 끝에 [ACTION:revise:산출물명:수정내용] 태그 추가
- GATE 승인 → 답변 끝에 [ACTION:approve] 태그 추가
- 실패 노드 확인 → 답변 끝에 [ACTION:failed] 태그 추가
- 일반 대화 → 태그 없이 자연스럽게 대화
- 이전 대화 맥락을 참고해서 "그것도", "아까 그거" 같은 지시어도 이해

사용자: {text}"""

    stdout, _ = await asyncio.wait_for(
        proc.communicate(input=prompt.encode("utf-8")),
        timeout=60,
    )
    reply = stdout.decode("utf-8").strip()

    # JSON 응답이면 result 추출
    try:
        d = json.loads(reply)
        reply = d.get("result", reply)
    except Exception:
        pass

    # ACTION 태그 처리
    import re
    action_match = re.search(r'\[ACTION:(\w+)(?::([^:\]]+))?(?::([^\]]+))?\]', reply)
    clean_reply = re.sub(r'\[ACTION:[^\]]+\]', '', reply).strip()

    if clean_reply:
        _add_history(chat_id, "bot", clean_reply)
        await send(session, chat_id, clean_reply)

    if action_match:
        action = action_match.group(1)
        if action == "status":
            await cmd_status(session, chat_id)
        elif action == "failed":
            await cmd_failed(session, chat_id)
        elif action == "revise" and action_match.group(2):
            node_name = action_match.group(2)
            request_text = action_match.group(3) or text
            await cmd_revise(session, chat_id, f"{node_name}: {request_text}")
        elif action == "approve":
            await cmd_status(session, chat_id)


async def cmd_help(session, chat_id):
    await send(session, chat_id, """🤖 *v8 AI SI Platform Bot*

/status — 프로젝트 현황
/failed — 실패 노드 목록
/approve\\_XXXXXXXX — GATE 승인
/retry\\_XXXXXXXX — 노드 재시도
/revise 산출물명: 수정 내용 — 수정 요청
/help — 이 도움말""")


# ---------------------------------------------------------------------------
# 텔레그램 API
# ---------------------------------------------------------------------------

async def send(session, chat_id, text):
    text = text[:4096]
    async with session.post(f"{TELEGRAM_API}/sendMessage",
                            json={"chat_id": chat_id, "text": text,
                                  "parse_mode": "Markdown"}) as r:
        if r.status != 200:
            # Markdown 실패 시 plain text
            async with session.post(f"{TELEGRAM_API}/sendMessage",
                                    json={"chat_id": chat_id, "text": text}) as r2:
                pass


async def process_update(session, update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id")
    text = msg.get("text", "").strip()

    if not chat_id or not text:
        return

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await send(session, chat_id, "⛔ 권한이 없습니다.")
        return

    if text == "/start" or text == "/help":
        await cmd_help(session, chat_id)
    elif text == "/status":
        await cmd_status(session, chat_id)
    elif text == "/failed":
        await cmd_failed(session, chat_id)
    elif text.startswith("/approve_"):
        gate_id = text.replace("/approve_", "").strip()
        await cmd_approve(session, chat_id, gate_id)
    elif text.startswith("/retry_"):
        node_id = text.replace("/retry_", "").strip()
        await cmd_retry(session, chat_id, node_id)
    elif text.startswith("/revise "):
        await cmd_revise(session, chat_id, text[8:])
    else:
        # 자연어 → AI가 해석해서 처리
        await cmd_natural(session, chat_id, text)


# ---------------------------------------------------------------------------
# 폴링 루프
# ---------------------------------------------------------------------------

async def main():
    logger.info("v8 텔레그램 봇 시작")
    offset = 0
    async with aiohttp.ClientSession() as session:
        # 로그인
        await _login(session)
        logger.info("v8 API 로그인 완료")

        while True:
            try:
                async with session.get(f"{TELEGRAM_API}/getUpdates",
                                       params={"offset": offset, "timeout": 30}) as r:
                    data = await r.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        await process_update(session, update)
            except Exception as e:
                logger.error(f"폴링 오류: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
