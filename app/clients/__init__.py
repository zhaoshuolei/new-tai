"""外部服务客户端。"""

from app.clients.feishu import FeishuClient, FeishuUserInfo, get_feishu_client

__all__: tuple[str, ...] = ("FeishuClient", "FeishuUserInfo", "get_feishu_client")
