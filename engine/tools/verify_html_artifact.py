"""
engine/tools/verify_html_artifact.py

HTML 산출물의 글로벌 스타일 완정성 검증 도구.

V9가 생성한 화면 설계서/UI 시안 등의 HTML이 body/html/:root 레벨
글로벌 스타일을 갖추고 화면 박스 바깥 설명 텍스트도 스타일 상속을
받는지 확인한다. CI 또는 수동 QA로 사용 가능.

사용:
    PYTHONPATH=. python3 engine/tools/verify_html_artifact.py <file.html> [<file2.html> ...]

종료 코드:
    0 = 모든 파일 통과
    1 = 하나 이상 실패
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def check(html_path: Path) -> list[str]:
    """HTML 파일에 대한 품질 체크. 반환: 문제 리스트 (빈 리스트면 PASS)."""
    txt = html_path.read_text(encoding="utf-8", errors="replace")
    issues: list[str] = []

    # 1. 글로벌 selector 규칙 존재
    if not re.search(r'(?m)^\s*(body|html|\*|:root)\s*\{', txt):
        issues.append("글로벌 스타일 규칙 없음 (body/html/:root/* 중 하나 필요)")

    # 2. body 또는 html 에 background 선언
    body_block = re.search(r'(?s)(body|html)\s*\{([^}]*)\}', txt)
    if body_block:
        decls = body_block.group(2)
        if not re.search(r'background(-color)?\s*:', decls):
            issues.append(f"{body_block.group(1)} 규칙에 background 선언 없음")
        if not re.search(r'(^|;)\s*color\s*:', decls):
            issues.append(f"{body_block.group(1)} 규칙에 color 선언 없음")
    else:
        issues.append("body 또는 html selector 규칙 없음")

    # 3. Pretendard 또는 system font-family 선언 (V9 디자인 규칙)
    if not re.search(r'font-family\s*:\s*["\']?(Pretendard|Variable|-apple-system|system-ui)',
                     txt[:20000], re.IGNORECASE):
        issues.append("최상단 20KB 내 Pretendard/system-ui 폰트 패밀리 선언 없음")

    # 4. 최대 너비 컨테이너 (좌우 여백 확보)
    if not re.search(r'max-width\s*:\s*\d+', txt):
        issues.append("max-width 컨테이너 없음 (좌우 여백이 없어 텍스트 폭 무한 확장 위험)")

    # 5. 다크 테마 확인 (V9 화면 설계서 규칙)
    if re.search(r'background\s*:\s*#f[0-9a-f]{2,5}', txt[:10000], re.IGNORECASE):
        issues.append("상단 10KB 내 라이트 배경(#fxx) 발견 — 다크 테마 규칙 위반")

    # 6. 닫힘 태그 정합
    if txt.count("<body") != txt.count("</body>"):
        issues.append(f"<body> 개폐 불일치 ({txt.count('<body')} vs {txt.count('</body>')})")

    return issues


def main() -> int:
    if len(sys.argv) < 2:
        print("사용: verify_html_artifact.py <file.html> [...]")
        return 2

    overall_ok = True
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.is_file():
            print(f"[SKIP] {arg} (파일 없음)")
            continue
        problems = check(p)
        if problems:
            overall_ok = False
            print(f"[FAIL] {p.name}")
            for issue in problems:
                print(f"  - {issue}")
        else:
            print(f"[PASS] {p.name}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
