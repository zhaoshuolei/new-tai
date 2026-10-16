"""领域模型包，模型模块导入后向 Base.metadata 注册表定义。"""

from app.models.department import Department, DepartmentStatus

__all__: tuple[str, ...] = ("Department", "DepartmentStatus")
