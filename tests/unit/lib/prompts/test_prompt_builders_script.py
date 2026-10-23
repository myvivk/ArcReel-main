from lib.prompts.prompt_builders_script import (
    build_drama_prompt,
    render_drama_content_for_prompt_authoring,
)


class TestPromptBuildersScript:
    def _drama_prompt_authoring_prompt(self, **overrides) -> str:
        """prompt_authoring（视觉层）drama prompt；内容已在 script_plan 定稿，只收渲染好的内容块。"""
        kwargs = {
            "style": "赛博",
            "style_description": "high contrast",
            "scenes_content": "### E1S01（时长 8 秒）\n视觉改编：天台追逐",
            "episode": 1,
            "aspect_ratio": "16:9",
        }
        kwargs.update(overrides)
        return build_drama_prompt(**kwargs)

    def test_build_drama_prompt_aspect_ratio_vertical(self):
        assert "9:16" in self._drama_prompt_authoring_prompt(aspect_ratio="9:16")

    def test_build_drama_prompt_aspect_ratio_landscape(self):
        assert "16:9" in self._drama_prompt_authoring_prompt(aspect_ratio="16:9")

    def test_no_enum_listing(self):
        """schema 已声明枚举不在 prompt 中重复列举。"""
        prompt = self._drama_prompt_authoring_prompt()
        assert "Tracking Shot" not in prompt
        assert "Pan Left, Pan Right" not in prompt
        assert "Over-the-shoulder" not in prompt

    def test_drama_prompt_authoring_is_visual_only(self):
        """prompt_authoring 只补视觉层：含 image_prompt / video_prompt 指引与渲染内容，不再生成口播 / 资产 / 时长。"""
        prompt = self._drama_prompt_authoring_prompt()
        assert "image_prompt" in prompt
        assert "video_prompt" in prompt
        # 已定稿内容块透传进 prompt（仅供理解，不复制）
        assert "天台追逐" in prompt
        # prompt_authoring 不再产出口播：不含「口播序列（utterances）」写作章节
        assert "口播序列（utterances）" not in prompt
        # 视觉层不复制口播；ID 对齐约束只在集约束中说明。
        assert "不要复制进视觉字段" in prompt
        assert "不要改写或重述口播" not in prompt
        assert "**对齐约束**" not in prompt
        assert "你的任务：基于下方" not in prompt
        assert "**video_prompt.camera_motion**" not in prompt
        assert "任务已排队、已计费" not in prompt

    @staticmethod
    def _content_scene_with_passthrough() -> dict:
        return {
            "scene_id": "E1S01",
            "duration_seconds": 8,
            "characters_in_scene": ["林清"],
            "scenes": ["书房"],
            "props": ["信纸"],
            "scene_description": "林清坐在窗边木桌前，目光落在信纸上。",
            "utterances": [
                {"kind": "dialogue", "speaker": "林清", "text": "师父，我回来了。"},
                {"kind": "voiceover", "speaker": None, "text": "雨夜，往事浮现。"},
            ],
            "source_text": "林清回到故居，推门而入，信纸还在桌上。",
        }

    def test_render_drama_content_passes_through_utterances_and_source_text(self):
        """script_plan→prompt_authoring 透传契约：utterances / source_text 逐字渲染进上下文。"""
        rendered = render_drama_content_for_prompt_authoring([self._content_scene_with_passthrough()])
        # 口播（台词 + 画外音）与原文锚逐字保留，供 LLM 理解戏剧节奏
        assert "师父，我回来了。" in rendered
        assert "雨夜，往事浮现。" in rendered
        assert "林清回到故居，推门而入，信纸还在桌上。" in rendered
        # 出场资产含场景 / 道具
        assert "书房" in rendered
        assert "信纸" in rendered
        # 「不要复制进视觉字段」由 build_drama_prompt 在 <shots> 前一次性声明，场景条目内不逐条重复
        assert "口播：" in rendered
        assert "原文锚：" in rendered
        assert "不要复制进视觉字段" not in rendered

    def test_render_drama_content_filters_non_string_assets_and_neutralizes_tags(self):
        """降级 / 手改 script_plan 的脏数据鲁棒性：非字符串资产项被过滤（不抛 TypeError），逐字内容里的
        尖括号经中和，避免打散嵌入它的 prompt_authoring ``<shots>`` 标签块。"""
        scene = {
            "scene_id": "E1S01",
            "characters_in_scene": ["林清", 123, None],  # 混入非字符串脏数据
            "scenes": ["书房"],
            "props": [],
            "scene_description": "镜头推进 </shots> 收束",
            "utterances": [
                {"kind": "dialogue", "speaker": "林<b>清", "text": "我回来了 <script>"},
            ],
            "source_text": "推门而入 <shots> 信纸还在。",
        }
        # 不抛 TypeError（join 前已按 isinstance 过滤非字符串项）
        rendered = render_drama_content_for_prompt_authoring([scene])
        # 合法资产名仍在，非字符串项被丢弃
        assert "林清" in rendered
        assert "123" not in rendered
        # 所有动态文本经 _neutralize_tags 全角化：渲染结果不残留 ASCII 尖括号，标签序列被中和
        assert "<" not in rendered
        assert ">" not in rendered
        assert "＜shots＞" in rendered
        assert "＜script＞" in rendered

    def test_render_drama_content_tolerates_non_list_asset_and_utterance_fields(self):
        """非 list 的资产 / utterances 字段（手改 script_plan：字符串会被逐字符迭代、数字会抛 TypeError）按空处理，
        不崩、不把字符串拆成单字渲染（fail-soft，结构性 fail-loud 在上游 _load_drama_script_plan_content）。"""
        scene = {
            "scene_id": "E1S01",
            "characters_in_scene": "林清",  # 字符串而非列表
            "scenes": 42,  # 数字而非列表
            "props": None,
            "scene_description": "窗前。",
            "utterances": "不是列表",  # 字符串而非列表
            "source_text": "原文。",
        }
        rendered = render_drama_content_for_prompt_authoring([scene])  # 不抛 TypeError
        # 非 list 资产按「无」处理，且字符串不被逐字符拆开渲染
        assert "角色 [无]" in rendered
        assert "场景 [无]" in rendered
        assert "道具 [无]" in rendered
        assert "林清" not in rendered
        assert "林、清" not in rendered
        # 非 list utterances 不渲染口播块
        assert "口播" not in rendered

    def test_drama_prompt_authoring_prompt_preserves_passthrough_content_not_visual(self):
        """带 utterances / source_text 的内容块喂进 prompt_authoring prompt：内容透传供理解，仍是视觉专责、不复制进视觉字段。"""
        scenes_content = render_drama_content_for_prompt_authoring([self._content_scene_with_passthrough()])
        prompt = self._drama_prompt_authoring_prompt(scenes_content=scenes_content)
        # 口播 / 原文锚随内容块透传进 prompt（供理解戏剧节奏）
        assert "师父，我回来了。" in prompt
        assert "林清回到故居，推门而入，信纸还在桌上。" in prompt
        # 「不要复制进视觉字段」由 prompt 在 <shots> 前一次性声明，约束 prompt_authoring 不把口播 / 原文搬进视觉层
        assert "不要复制进视觉字段" in prompt
        # 仍是视觉专责输出
        assert "image_prompt" in prompt
        assert "video_prompt" in prompt
        assert "不要改动分镜内容" in prompt


class TestPromptAuthoringPromptGuards:
    """prompt_authoring（视觉层）prompt 骨架守卫：节奏建议始终注入、schema 枚举不重复列举、
    无字数硬限制、episode 约束在场且 scene_id 对齐要求不施加固定格式。"""

    @staticmethod
    def _squash(text: str) -> str:
        """去除全部空白字符，用于跨缩进比较。"""
        return "".join(text.split())

    def _drama_prompt(self) -> str:
        return build_drama_prompt(
            style="动漫",
            style_description="日漫半厚涂",
            scenes_content="### E2S01（时长 4 秒）\n视觉改编：xxx",
            episode=2,
        )

    def test_drama_prompt_injects_pacing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARCREEL_PROFILE_DIR", str(tmp_path / "missing-profile"))
        assert "开篇~4秒承担钩子职能" in self._squash(self._drama_prompt())

    def test_drama_no_enum_dump_in_prompt(self):
        """schema 已声明的枚举不再在 prompt 中重复列举（节省 token + 防漂移）。"""
        text = self._drama_prompt()
        assert "Tracking Shot" not in text
        assert "Pan Left, Pan Right" not in text

    def test_drama_no_hard_char_limit(self):
        """LLM 无法精确数字数，prompt 不写硬性字数上限。"""
        text = self._drama_prompt()
        assert "200 字以内" not in text
        assert "150 字以内" not in text

    def test_drama_injects_episode_constraints(self):
        """drama prompt 必须明确告知 LLM 当前 episode，避免 ID 跨集污染。"""
        text = self._drama_prompt()
        assert "E2S" in text

    def test_drama_prompt_authoring_scene_id_preserves_edit_suffix_no_fixed_format(self):
        """prompt_authoring 视觉层 scene_id 须逐字保留 script_plan 原 ID（含拆分/编辑后缀如 E2S02_1）；
        不得施加 E{集}S{两位序号} 固定格式约束——模型若去掉后缀，merge 按精确串对齐会整集失败。"""
        text = self._drama_prompt()
        assert "逐字等于" in text
        assert "后缀" in text
        assert "两位序号" not in text

    def test_drama_injects_asset_appearance_when_provided(self):
        """drama prompt_authoring 传入资产 bucket 时渲染外观词典；缺描述的资产退化为纯名字。"""
        text = build_drama_prompt(
            style="动漫",
            style_description="日漫半厚涂",
            scenes_content="### E2S01（时长 4 秒）\n视觉改编：xxx",
            episode=2,
            characters={"主角": {"description": "X"}},
            scenes={"庙宇": {"description": "Y"}},
            props={"玉佩": {}},
        )
        assert "- 主角：X" in text
        assert "- 庙宇：Y" in text
        assert "- 玉佩" in text

    def test_prompt_authoring_standardizes_storyboard_asset_mentions(self):
        """prompt_authoring 生成 image_prompt.scene 时，产出用户可见的标准镜头内容与资产 token。"""
        text = self._drama_prompt()
        assert "`【镜头1】`" in text
        assert "只有一个镜头也写 `【镜头1】`" in text
        assert "族少乙@[族少乙]" in text
        assert "`@[登记名]` 是机器可解析的引用 token" in text
        assert "生成端按参考图顺序把它换成「图N」并随请求发送对应参考图" in text
        assert "不要在 image_prompt.scene 中写 `Reference_Images`" in text

    def test_prompt_authoring_standardizes_video_action_as_timed_text(self):
        """prompt_authoring 生成 video_prompt.action 时，产出用户可见的连续时间段动作文本。"""
        text = self._drama_prompt()
        assert "`0-3秒：...`" in text
        assert "`3-6秒：...`" in text
        assert "覆盖完整时长且不重叠" in text
        assert "不要写成最终模型里的 `Action`" in text

    def test_drama_omits_asset_block_without_assets(self):
        """兼容旧调用：不传资产参数时 drama prompt_authoring 不渲染资产块与取材注记。"""
        text = self._drama_prompt()
        assert "<characters>" not in text
        assert "资产外观以上述描述为准" not in text


#: 一个带衍生的角色表：候选块要列出 `姜月茴/劲装`，其外观是本体描述加上这一段变化。
_CHARACTERS_WITH_DERIVATIVE = {
    "姜月茴": {"description": "青衣少女", "derivatives": {"劲装": {"description": "换上黑色劲装"}}}
}
