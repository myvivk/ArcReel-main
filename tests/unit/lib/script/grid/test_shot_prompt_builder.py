"""分镜段到关键帧的映射必须覆盖整个段落，不能丢掉结尾。"""

from lib.script.grid.keyframe_parser import ShotFrame
from lib.script.grid.shot_prompt_builder import resolve_grid_cells, shot_grid_cells


def test_more_narrative_shots_than_cells_keeps_every_event_in_order():
    frames = [ShotFrame(number=index + 1, shot_size="近景", text=f"独立事件{index}") for index in range(7)]
    resolved = resolve_grid_cells(frames, 4)
    assert len(resolved) == 4
    assert "\n".join(frame.text for frame in resolved) == "\n".join(frame.text for frame in frames)
    assert [frame.number for frame in resolved] == [1, 2, 3, 4]
    cells = shot_grid_cells(frames, 2, 2)
    assert [cell.prompt for cell in cells] == [frame.text for frame in resolved]


def test_fewer_narrative_shots_extend_the_last_action_without_new_events():
    resolved = resolve_grid_cells([ShotFrame(number=1, shot_size="全景", text="角色推门走入房间")], 4)
    assert len(resolved) == 4
    assert resolved[0].text == "角色推门走入房间"
    assert all("不新增事件、不切换场景" in frame.text for frame in resolved[1:])
