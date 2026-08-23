"""展示层格式化工具，供 Jinja 模板使用。"""
import re

_SAFE_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")

# 兼容切换 ip-api 中文响应前已保存的英文地区数据。
_LEGACY_LOCATION_NAMES = {
    "Guangxi": "广西",
    "Gangkou": "港口区",
}


def location_label(province, city):
    """格式化 IP 地区，兼容历史英文地区值。"""
    province = _LEGACY_LOCATION_NAMES.get(
        (province or "").strip(), (province or "").strip()
    )
    city = _LEGACY_LOCATION_NAMES.get(
        (city or "").strip(), (city or "").strip()
    )
    return f"{province}{city}" if province or city else "未知地区"


def safe_color(value, default="#6c5ce7"):
    """清洗主题色，仅接受合法的十六进制颜色，防止 CSS/HTML 注入。"""
    v = str(value).strip() if value else ""
    if _SAFE_COLOR_RE.match(v):
        return v
    return default


def uploader_label(row):
    """根据图片记录生成上传者展示文案。

    统一在主体前添加地区标识，署名与匿名一致：
      - 有省/市： "{省}{市}的{主体}"，如 "广西柳州的张三" / "广西柳州的匿名用户"
      - 海外：    "海外地区的{主体}"
      - 未知：    "未知地区的{主体}"
    主体为署名（author 非空）或 "匿名用户"。
    """
    province = (row["province"] or "").strip()
    city = (row["city"] or "").strip()
    author = (row["author"] or "").strip()
    region = (row["region"] or "").strip() if "region" in row.keys() else ""

    if province or city:
        place = location_label(province, city)
    elif region == "overseas":
        place = "海外地区"
    else:
        place = "未知地区"

    subject = author if author else "匿名用户"
    return f"{place}的{subject}"
