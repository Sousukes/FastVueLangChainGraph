"""阶段 00 验证脚本：确认 DeepSeek Key 有效、网络可达。

运行：
    cd backend
    uv venv && uv pip install openai python-dotenv
    uv run python verify_key.py
"""
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()  # 读取 backend/.env

client = OpenAI(
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
    api_key=os.getenv("DEEPSEEK_API_KEY"),
)

resp = client.chat.completions.create(
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    messages=[{"role": "user", "content": "用一句话介绍你自己"}],
)

print(resp.choices[0].message.content)
