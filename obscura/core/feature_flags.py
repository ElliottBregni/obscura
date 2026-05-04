"""obscura.core.feature_flags — Hard-coded feature flag config store.

Edit the FLAGS instance at the bottom of this file to change behaviour.
No restart required — changes take effect on the next process start.

Banner themes
-------------
OBSCURA_DEFAULT        Classic oscillating purple->blue OBSCURA block letters (original)
OVERHAUL_GREEN_BLUE    Block letters oscillating green <-> blue  (Overhaul site palette)
OVERHAUL_ORANGE        Block letters solid orange              (Overhaul brand colour)
OBSCURA_BY_OVERHAUL    "OBSCURA" oscillating cyan/teal + "by OVERHAUL" green/blue subtitle
NONE                   No banner at all
"""

from __future__ import annotations

from dataclasses import dataclass

from obscura.core.enums.ui import BannerTheme


@dataclass(frozen=True)
class FeatureFlags:
    # -- Banner ---------------------------------------------------------------
    banner_enabled: bool = True
    banner_theme: BannerTheme = BannerTheme.OVERHAUL_GREEN_BLUE  # <- change this

    # -- Debug / dev flags ----------------------------------------------------
    debug_tool_calls: bool = False
    stream_thinking: bool = False


# ----------------------------------------------------------------
# HARD-CODED CONFIG -- edit these values to flip behaviour
#
#  banner_theme options:
#    BannerTheme.OBSCURA_DEFAULT      (purple/blue OBSCURA)
#    BannerTheme.OVERHAUL_GREEN_BLUE  (green/blue OVERHAUL)
#    BannerTheme.OVERHAUL_ORANGE      (orange OVERHAUL)
#    BannerTheme.OBSCURA_BY_OVERHAUL  (OBSCURA by OVERHAUL)
#    BannerTheme.NONE                 (no banner)
# ----------------------------------------------------------------
FLAGS = FeatureFlags(
    banner_enabled=True,
    banner_theme=BannerTheme.NONE,  # dark blue + Irish green + cat
)
