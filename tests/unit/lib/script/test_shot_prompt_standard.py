"""标准分镜提示词格式闸（``lib/script/shot_prompt_standard``）的单元测试。

覆盖统一标准（D-3 / D-10）的封闭词表与 ``lint_shot_prompt`` 各校验分支：镜头标记、景别 /
机位 / 运镜、丰满度、token 硬闸（登记 + 出场清单）、泛化前缀、凑数镜头。
"""

from lib.script.shot_prompt_standard import lint_shot_prompt, shot_blocks

#: 一个合规的单镜头提示词（裸文本含动作 + 神态，标记带景别，首句机位 + 运镜）。
_GOOD = (
    "【镜头1｜中景】平视机位，缓推。林清@[林清]攥紧门把手，侧耳贴门屏息，指节因用力而发白，呼吸压在喉咙里；"
    "旧公寓@[旧公寓]的水泥楼道里声控灯忽明忽暗，铁门@[铁门]的锈迹映出她绷紧的侧脸，一缕冷光扫过门缝。"
)

_ASSETS = {"林清", "旧公寓", "铁门"}


def test_good_prompt_passes():
    assert lint_shot_prompt(_GOOD, registered_names=_ASSETS, allowed_names=_ASSETS) is None


def test_missing_shot_mark_is_rejected():
    assert "没有【镜头N】" in lint_shot_prompt(
        "平视机位，缓推。林清攥紧门把手，侧耳贴门屏息。", registered_names=_ASSETS
    )


def test_shot_size_is_required_in_mark():
    scene = _GOOD.replace("【镜头1｜中景】", "【镜头1】")
    assert "缺景别" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_camera_position_and_motion_required_in_first_sentence():
    for header, problem in (("【镜头1｜中景】缓推。", "缺机位"), ("【镜头1｜中景】平视机位。", "缺运镜")):
        scene = header + _GOOD.split("。", 1)[1]
        assert problem in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_placeholder_phrase_is_rejected():
    scene = _GOOD.replace("林清@[林清]", "一位人物@[林清]")
    assert "空泛占位" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_unregistered_token_is_rejected():
    scene = _GOOD.replace("林清@[林清]", "路人@[路人]")
    assert "未登记资产" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_token_outside_appearance_list_is_rejected():
    scene = _GOOD.replace("林清@[林清]", "铁门@[铁门]")  # 铁门已登记，但不在出场清单
    assert "出场资产清单之外" in lint_shot_prompt(scene, registered_names=_ASSETS, allowed_names={"林清", "旧公寓"})


def test_generic_prefix_is_rejected():
    scene = _GOOD.replace("林清@[林清]", "角色@[林清]")
    assert "泛化前缀" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_token_stuffed_with_appearance_is_rejected():
    scene = _GOOD.replace("林清@[林清]", "林清@[林清，长发及腰]")
    assert "外观描述" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_derivative_token_checks_owner_name():
    scene = _GOOD.replace("林清@[林清]", "林清@[林清/劲装]")
    assert lint_shot_prompt(scene, registered_names=_ASSETS, allowed_names=_ASSETS) is None


def test_shot_numbers_must_be_continuous():
    scene = _GOOD + "\n\n" + _GOOD.replace("【镜头1｜中景】", "【镜头3｜中景】")
    assert "连续" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_duplicate_shots_are_rejected():
    scene = _GOOD + "\n\n" + _GOOD.replace("【镜头1｜中景】", "【镜头2｜中景】")
    assert "凑数镜头" in lint_shot_prompt(scene, registered_names=_ASSETS)


def test_shot_blocks_split_by_mark():
    scene = "【镜头1｜中景】平视机位，缓推。a" + "\n\n" + "【镜头2｜特写】仰拍机位，固定。b"
    blocks = shot_blocks(scene)
    assert [block.number for block in blocks] == [1, 2]
    assert [block.shot_size for block in blocks] == ["中景", "特写"]
