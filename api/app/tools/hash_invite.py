"""生成 INVITE_CODE_HASH。

    python -m api.app.tools.hash_invite

明文邀请码不进 .env、不进 git、不进 GitHub secrets —— 只有它的 argon2 hash 会。
"""

from __future__ import annotations

import getpass
import sys

from api.app.auth import hash_invite_code


def main() -> int:
    # 用 getpass 而不是 argv：避免邀请码留在 shell history 和进程列表里
    code = getpass.getpass("邀请码: ")
    if not code:
        print("邀请码不能为空", file=sys.stderr)
        return 1
    if code != getpass.getpass("再输一次: "):
        print("两次输入不一致", file=sys.stderr)
        return 1
    # 直接输出可整行粘贴进 .env 的形式。单引号必不可少：argon2 hash 里全是 $，
    # docker compose 读 .env 时会把 $argon2id / $v / $m 当变量引用替换成空串，
    # 塞给容器一个被啃烂的 hash —— 登录会 500。单引号让 compose 和
    # pydantic-settings 都按字面处理。
    print(f"INVITE_CODE_HASH='{hash_invite_code(code)}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
