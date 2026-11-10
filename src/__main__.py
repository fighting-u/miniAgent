"""项目终端入口：在项目根目录执行 python -m src。"""

import asyncio

from src.agent.core import main


if __name__ == "__main__":
    asyncio.run(main())
