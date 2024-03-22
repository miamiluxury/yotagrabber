"""Get a list of Toyota vehicles from the Toyota website."""
import datetime
import json
import os
import sys
import uuid
from functools import cache
from secrets import randbelow
from time import sleep
from timeit import default_timer as timer

import pandas as pd
import requests

from yotagrabber import config, wafbypass

# Set to True to use local data and skip requests to the Toyota website.
USE_LOCAL_DATA_ONLY = False

# Get the model that we should be searching for.
MODEL = os.environ.get("MODEL")


@cache
def get_vehicles_query(zone="west"):
    """Read vehicles query from a file."""
    with open(f"{config.BASE_DIRECTORY}/graphql/vehicles.graphql", "r") as fileh:
        query = fileh.read()

    zip_codes = {
        "west": "84101",  # Salt Lake City
        "central": "73007",  # Oklahoma City
        "east": "27608",  # Raleigh
    }

    # Replace certain place holders in the query with values.
    zip_code = zip_codes[zone]
    query = query.replace("ZIPCODE", zip_code)
    query = query.replace("MODELCODE", MODEL)
    query = query.replace("DISTANCEMILES", str(5823 + randbelow(1000)))
    query = query.replace("LEADIDUUID", str(uuid.uuid4()))

    return query


def read_local_data():
    """Read local raw data from the disk instead of querying Toyota."""
    return pd.read_parquet(f"output/{MODEL}_raw.parquet")


def query_toyota(page_number, query, headers):
    """Query Toyota for a list of vehicles."""

    # Replace the page number in the query
    query = query.replace("PAGENUMBER", str(page_number))

    # Make request.
    json_post = {"query": query}
    url = "https://api.search-inventory.toyota.com/graphql"
    resp = requests.post(
        url,
        json=json_post,
        headers=headers,
        timeout=15,
    )

    try:
        result = resp.json()["data"]["locateVehiclesByZip"]
    except requests.exceptions.JSONDecodeError:
        print(resp.headers)
        print(resp.text)
        return None

    if not result or "vehicleSummary" not in result:
        print(resp.text)
        return None
    else:
        return result


def get_all_pages():
    """Get all pages of results for a query to Toyota."""
    df = pd.DataFrame()
    page_number = 1

    # Read the query.
    west_query = get_vehicles_query(zone="west")
    central_query = get_vehicles_query(zone="central")
    east_query = get_vehicles_query(zone="east")

    # Get headers by bypassing the WAF.
    print("Bypassing WAF")
    headers = wafbypass.WAFBypass().run()

    # Start a timer.
    timer_start = timer()

    # Set a last run counter.
    last_run_counter = 0

    while True:
        # Toyota's API won't return any vehicles past past 40.
        if page_number > 40:
            break

        # The WAF bypass expires every 5 minutes, so we refresh about every 4 minutes.
        elapsed_time = timer() - timer_start
        if elapsed_time > 4 * 60:
            print("  >>> Refreshing WAF bypass >>>\n")
            headers = wafbypass.WAFBypass().run()
            timer_start = timer()

        # Get a page of vehicles.
        print(f"Getting page {page_number} of {MODEL} vehicles")

        west_result = query_toyota(page_number, west_query, headers)
        if west_result and "vehicleSummary" in west_result:
            print("West:    ", len(west_result["vehicleSummary"]))
            df = pd.concat([df, pd.json_normalize(west_result["vehicleSummary"])])

        central_result = query_toyota(page_number, central_query, headers)
        if central_result and "vehicleSummary" in central_result:
            print("Central: ", len(central_result["vehicleSummary"]))
            df = pd.concat([df, pd.json_normalize(central_result["vehicleSummary"])])

        east_result = query_toyota(page_number, east_query, headers)
        if east_result and "vehicleSummary" in east_result:
            print("East:    ", len(east_result["vehicleSummary"]))
            df = pd.concat([df, pd.json_normalize(east_result["vehicleSummary"])])

        # Drop any duplicate VINs.
        df.drop_duplicates(subset=["vin"], inplace=True)

        print(f"Found {len(df)} (+{len(df)-last_run_counter}) vehicles so far.\n")

        # If we didn't find more cars from the previous run, we've found them all.
        if len(df) == last_run_counter:
            print("All vehicles found.")
            break

        last_run_counter = len(df)
        page_number += 1

        sleep(10)
        continue

    return df


def update_vehicles():
    """Generate a curated database of vehicles."""
    if not MODEL:
        sys.exit("Set the MODEL environment variable first")

    df = read_local_data() if USE_LOCAL_DATA_ONLY else get_all_pages()

    # Stop here if there are no vehicles to list.
    if df.empty:
        print(f"No vehicles found for model: {MODEL}")
        return

    # Write the raw data to a file.
    if not USE_LOCAL_DATA_ONLY:
        df.sort_values("vin", inplace=True)
        df.to_parquet(f"output/{MODEL}_raw.parquet", index=False)

    # Add dealer data.
    dealers = pd.read_csv(f"{config.BASE_DIRECTORY}/data/dealers.csv")[
        ["dealerId", "state"]
    ]
    dealers.rename(columns={"state": "Dealer State"}, inplace=True)
    df["dealerCd"] = df["dealerCd"].apply(pd.to_numeric)
    df = df.merge(dealers, left_on="dealerCd", right_on="dealerId")

    renames = {
        "vin": "VIN",
        "price.baseMsrp": "Base MSRP",
        "price.totalMsrp": "TSRP MSRP",
        "model.marketingName": "Model",
        "extColor.marketingName": "Color",
        "dealerCategory": "Shipping Status",
        "dealerMarketingName": "Dealer",
        # "dealerWebsite": "Dealer Website",
        "isPreSold": "Pre-Sold",
        "holdStatus": "Hold Status",
        "year": "Year",
        "drivetrain.code": "Drivetrain",
        # "options": "Options",
    }

    with open(f"output/models.json", "r") as fileh:
        title = [x["title"] for x in json.load(fileh) if x["modelCode"] == MODEL][0]

    df = (
        df[
            [
                "vin",
                "dealerCategory",
                #"price.baseMsrp",
                "price.totalMsrp",
                #"price.dioTotalDealerSellingPrice",
                "isPreSold",
                "holdStatus",
                "year",
                "drivetrain.code",
                # "media",
                "model.marketingName",
                "extColor.marketingName",
                "dealerMarketingName",
                # "dealerWebsite",
                "Dealer State",
                "options",
            ]
        ]
        .copy(deep=True)
        .rename(columns=renames)
    )

    # Remove the model name (like 4Runner) from the model column (like TRD Pro).
    df["Model"] = df["Model"].str.replace(f"{title} ", "")

    # Clean up missing colors and colors with extra tags.
    df = df[df["Color"].notna()]
    df["Color"] = df["Color"].str.replace(" [extra_cost_color]", "", regex=False)

    # Calculate the dealer price + markup.
    df["Dealer Price"] = df["Base MSRP"] + df["price.dioTotalDealerSellingPrice"]
    df["Dealer Price"] = df["Dealer Price"].fillna(df["Base MSRP"])
    df["Markup"] = df["Dealer Price"] - df["Base MSRP"]
    df.drop(columns=["price.dioTotalDealerSellingPrice"], inplace=True)

    # Remove any old models that might still be there.
    last_year = datetime.date.today().year - 1
    df.drop(df[df["Year"] < last_year].index, inplace=True)

    statuses = {None: False, 1: True, 0: False}
    df.replace({"Pre-Sold": statuses}, inplace=True)

    statuses = {
        "A": "Factory to port",
        "F": "Port to dealer",
        "G": "At dealer",
    }
    df.replace({"Shipping Status": statuses}, inplace=True)

    # df["Image"] = df["media"].apply(
    #     lambda x: [x["href"] for x in x if x["type"] == "carjellyimage"][0]
    # )
    # df.drop(columns=["media"], inplace=True)

    # df["Options"] = df["Options"].apply(extract_marketing_long_names)

    # Add the drivetrain to the model name to reduce complexity.
    df["Model"] = df["Model"] + " " + df["Drivetrain"]

    df = df[
        [
            "Year",
            "Model",
            "Color",
            #"Base MSRP",
            "TSRP MSRP",
            #"Markup",
            #"Dealer Price",
            "Shipping Status",
            "Pre-Sold",
            "Hold Status",
            "VIN",
            "Dealer",
            # "Dealer Website",
            "Dealer State",
            # "Image",
             "Options",
        ]
    ]

    # Write the data to a file.
    df.sort_values(by=["VIN"], inplace=True)
    df.to_csv(f"output/{MODEL}.csv", index=False)


def extract_marketing_long_names(options_raw):
    """extracts `marketingName` from `Options` col"""
    options = set()
    for item in options_raw:
        if item.get("marketingName"):
            options.add(item.get("marketingName"))
        elif item.get("marketingLongName"):
            options.add(item.get("marketingLongName"))
        else:
            continue

    return " | ".join(sorted(options))
