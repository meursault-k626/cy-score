import pandas as pd
from pybaseball import pitching_stats, chandwick_register
from pybaseball import cache

# add cache layer to skip re-download in second run
cache.enable()

# get raw pitching_stats df from fangraphs

raw_fangraphs = pitching_stats(2015, 2025, qual=0, ind=1)