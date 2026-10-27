"""探针：一场勾引/调戏停手后，没有高潮的场次应当刺激性欲上升，而不是按结算降压。

基线（修改前）：delta=-0.520123、phase=recovering → 期望三项都失败。
目标（修改后）：delta>0、不进 recovering、仍然 0 次高潮 → 期望三项匹配。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from life.drives import DriveConfig, advance_to, initial_state


def main() -> dict:
    cfg = DriveConfig()
    now = 1792986700.0

    st = initial_state("probe|scope", cfg)
    d = st.drives["libido"]
    d.tension = 0.90
    d.stimulation = 0.30
    d.stimulation_peak = 0.50
    d.stimulation_at = now - 3600.0      # 一小时没推进，已过 45 分钟结算线
    d.scene_climaxes = 0                 # 本场一次都没到顶
    d.phase = "high"
    d.high_since = now - 7200.0
    d.high_elapsed_s = 7200.0
    st.updated_at = now - 3600.0

    out = advance_to(st, now, cfg, online_seconds=0.0)
    lib = out.drives["libido"]
    delta = round(lib.tension - 0.90, 6)
    return {
        "delta_positive": bool(delta > 0),
        "climaxed": bool(lib.scene_climaxes > 0),
        "recovering": lib.phase == "recovering",
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
