"""抓「函数体内 LOAD_GLOBAL 了一个从未导入的名字」—— `import main` 抓不到这类错误。

为什么需要它（阶段 16 的 P0 教训，阶段 17 起纳入标准验收）
--------------------------------------------------------
Python 的**函数体全局名在「调用时」才解析**，模块导入阶段只编译、不解析。
所以下面这些检查可以**全部通过**，而端点依然会在第一次被调用时炸掉：

    import main 成功、路由数正常、静态测试全绿、vue-tsc 0 错、vite build 通过
    —— 而 main.vision_stream() 依然抛 NameError: name 'vision' is not defined

阶段 16 就真的漏写过一行 `import vision`，bug 完整活到了用户第一次点按钮。
本脚本用 `dis` 扫一遍字节码，把这类错误**在验收阶段**就抓出来。

用法（在 backend/ 下）
----------------------
    uv run python _check_globals.py            # 默认查 main
    uv run python _check_globals.py main rag   # 也可以指定别的模块
    期望输出：MISSING: none

读结果
------
只要 MISSING 里出现任何模块名（形如 `{'vision_run': ['vision']}`），
就说明 `main.py` 少了一行 `import <模块>` —— **补上它**，
而不是去改端点里的调用。补的时候顺手加一行注释说明「漏了 import main 照样成功，
首次调用才 500」，防止后人再把它删掉。
"""

import builtins
import dis
import os
import sys
import types

# ⚠️ `python <脚本路径>` 会把 sys.path[0] 设成【脚本所在目录】而不是当前工作目录。
#    这里把 cwd 插到最前面，于是只要在 backend/ 下跑就一定能 import 到 main，
#    不必依赖 PYTHONPATH=. 之类的额外环境设置。
sys.path.insert(0, os.getcwd())


def walk(code):
    """递归遍历 code object（函数里还有嵌套函数 / 推导式，它们也是独立 code object）。"""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from walk(const)


def check(module_name: str) -> dict[str, list[str]]:
    """返回 {函数名: [未导入的全局名, ...]}；空 dict 表示干净。"""
    mod = __import__(module_name)
    bound = set(vars(mod)) | set(dir(builtins))

    # ⚠️ 必须只查【定义在本模块内】的函数：否则会把 import 进来的其他模块里的全局引用
    #    一并算进来（如 fastapi / pydantic 内部符号），产生大量误报。
    #    判据是 co_filename 是否等于本模块的 __file__。
    own_file = getattr(mod, "__file__", None)

    missing: dict[str, list[str]] = {}
    for name, obj in vars(mod).items():
        code = getattr(obj, "__code__", None)
        if code is None:
            continue
        if own_file and getattr(code, "co_filename", None) != own_file:
            continue
        for block in walk(code):
            for ins in dis.get_instructions(block):
                if ins.opname == "LOAD_GLOBAL" and ins.argval not in bound:
                    missing.setdefault(name, set()).add(ins.argval)

    return {k: sorted(v) for k, v in missing.items()}


if __name__ == "__main__":
    targets = sys.argv[1:] or ["main"]
    failed = False
    for target in targets:
        result = check(target)
        print(f"MISSING({target}):", result or "none")
        failed = failed or bool(result)
    # 非 0 退出码，方便脚本化调用 / 挂到 CI 上
    raise SystemExit(1 if failed else 0)
