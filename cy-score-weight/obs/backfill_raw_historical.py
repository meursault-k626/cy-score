from pybaseball import statcast
from datetime import date, timedelta
import pandas as pd
import ssl
import urllib3
import warnings

# tell python to skip SSL certificate verification (statcast go through HTTPS)
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# regular season range, excluding ASG
SEASON_RANGES = {
    2015: [(date(2015, 4, 5), date(2015, 7, 12)),
           (date(2015, 7, 17), date(2015, 10, 4))],
    2016: [(date(2016, 4, 3), date(2016, 7, 10)),
           (date(2016, 7, 15), date(2016, 10, 2))],
    2017: [(date(2017, 4, 2), date(2017, 7, 9)),
           (date(2017, 7, 14), date(2017, 10, 1))],
    2018: [(date(2018, 3, 29), date(2018, 7, 15)),
           (date(2018, 7, 19), date(2018, 10, 1))],
    2019: [(date(2019, 3, 20), date(2019, 7, 7)),
           (date(2019, 7, 11), date(2019, 9, 29))],
    2020: [(date(2020, 7, 23), date(2020, 9, 27))],
    2021: [(date(2021, 4, 1), date(2021, 7, 11)),
           (date(2021, 7, 16), date(2021, 10, 3))],
    2022: [(date(2022, 4, 7), date(2022, 7, 17)),
           (date(2022, 7, 21), date(2022, 10, 5))],
    2023: [(date(2023, 3, 30), date(2023, 7, 9)),
           (date(2023, 7, 14), date(2023, 10, 1))],
    2024: [(date(2024, 3, 20), date(2024, 7, 14)),
           (date(2024, 7, 19), date(2024, 9, 30))],
    2025: [(date(2025, 3, 18), date(2025, 7, 13)),
           (date(2025, 7, 18), date(2025, 9, 28))],
}



def main():

    for year, ranges in SEASON_RANGES.items():

        # split scraping into weekly batch
        # long, linear scrape can be unstable 
        dfs = []
        for start, end in ranges:
            # create time range based on SEASON_RANGES
            cursor = start
            while cursor <= end:

                # cap it to one week
                chunk_end = min(cursor + timedelta(days=6), end)

                print(f"Pulling {cursor} to {chunk_end}")

                df = statcast(start_dt=str(cursor), end_dt=str(chunk_end))

                if df is not None and not df.empty:
                    dfs.append(df)

                # advance one day to start a new week
                cursor = chunk_end + timedelta(days=1)


        # concat weekly to create yearly
        df_all = pd.concat(dfs, ignore_index = True)

        df_all.to_parquet(f"cy-score-weight/{year}_raw.parquet", index=False)

        print(f"Saved {len(df_all)} rows for {year}")


if __name__ == "__main__":
    main()



