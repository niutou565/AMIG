"""展示层格式化工具，供 Jinja 模板使用。"""


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
        place = f"{province}{city}"
    elif region == "overseas":
        place = "海外地区"
    else:
        place = "未知地区"

    subject = author if author else "匿名用户"
    return f"{place}的{subject}"
