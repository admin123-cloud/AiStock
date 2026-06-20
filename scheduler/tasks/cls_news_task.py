from __future__ import annotations

from utils.logger import get_logger


logger = get_logger("ClsNewsTask")


class ClsNewsTask:
    """Refresh CLS telegraph news and build lightweight event signals."""

    def execute(self, pages: int = 1, rn: int = 50):
        from services.cls_news_service import fetch_store_and_score_cls_news

        result = fetch_store_and_score_cls_news(pages=pages, rn=rn)
        logger.info(f"CLS news refresh finished: {result}")
        return result
