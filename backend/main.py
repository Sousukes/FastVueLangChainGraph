"""课程骨架入口（阶段 00）。

本文件仅用于验证后端环境可启动。阶段 01 将在此之上加入自封装 LLM client 与 /chat 接口。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AI 研究助手 · 课程骨架")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "msg": "骨架已就绪，阶段 01 将接入 LLM"}
