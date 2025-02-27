import json

import click
import sys
from ._cli_common import main, click_range2d


@main.command("save-tasks")
@click.option(
    "--grid",
    type=str,
    help=(
        "Grid name or spec: au-{10|20|30|60},africa-{10|20|30|60}, albers-au-25 (legacy one)"
        "'crs;pixel_resolution;shape_in_pixels'"
    ),
    prompt="""Enter GridSpec
 one of au-{10|20|30|60}, africa-{10|20|30|60}, albers_au_25 (legacy one)
 or custom like 'epsg:3857;30;5000' (30m pixels 5,000 per side in epsg:3857)
 >""",
    default=None,
)
@click.option(
    "--year",
    type=int,
    help="Only extract datasets for a given year. This is a shortcut for --temporal-range=<int>--P1Y",
)
@click.option(
    "--temporal-range",
    type=str,
    help="Only extract datasets for a given time range, Example '2020-05--P1M' month of May 2020",
)
@click.option(
    "--frequency",
    type=str,
    help="Specify temporal binning: annual|annual-fy|semiannual|seasonal|nov-mar|apr-oct|all",
)
@click.option("--env", "-E", type=str, help="Datacube environment name")
@click.option(
    "-z",
    "complevel",
    type=int,
    default=6,
    help="Compression setting for zstandard 1-fast, 9+ good but slow",
)
@click.option(
    "--overwrite", is_flag=True, default=False, help="Overwrite output if it exists"
)
@click.option(
    "--tiles", help='Limit query to tiles example: "0:3,2:4"', callback=click_range2d
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    hidden=True,
    help="Dump debug data to pickle",
)
@click.option(
    "--gqa",
    type=float,
    help="Only save datasets that pass `gqa_iterative_mean_xy <= gqa` test",
)
@click.option(
    "--usgs-collection-category",
    type=str,
    help="Only save datasets that pass `collection_category == usgs_collection_category` test",
)
@click.option(
    "--dataset-filter",
    type=str,
    default=None,
    help='Filter to apply on datasets - {"collection_category": "T1"}'
)
@click.argument("products", type=str, nargs=1)
@click.argument("output", type=str, nargs=1, default="")
def save_tasks(
    grid,
    year,
    temporal_range,
    frequency,
    output,
    products,
    dataset_filter,
    env,
    complevel,
    overwrite=False,
    tiles=None,
    debug=False,
    gqa=None,
    usgs_collection_category=None,
):
    """
    Prepare tasks for processing (query db).

    <todo more help goes here>

    \b
    Not yet implemented features:
      - output product config

    """
    from datacube import Datacube
    from .tasks import SaveTasks
    from .model import DateTimeRange

    filter = {}
    if dataset_filter:
        filter = json.loads(dataset_filter)

    if temporal_range is not None and year is not None:
        print("Can only supply one of --year or --temporal_range", file=sys.stderr)
        sys.exit(1)

    if temporal_range is not None:
        try:
            temporal_range = DateTimeRange(temporal_range)
        except ValueError:
            print(f"Failed to parse supplied temporal_range: '{temporal_range}'")
            sys.exit(1)

    if year is not None:
        temporal_range = DateTimeRange.year(year)

    if frequency is not None:
        if frequency not in ("annual", "annual-fy", "semiannual", "seasonal", "nov-mar", "apr-oct", "all"):
            print(f"Frequency must be one of annual|annual-fy|semiannual|seasonal|nov-mar|apr-oct|all and not '{frequency}'")
            sys.exit(1)

    if output == "":
        if temporal_range is not None:
            output = f"{products}_{temporal_range.short}.db"
        else:
            output = f"{products}_all.db"

    try:
        tasks = SaveTasks(
            output, grid, frequency=frequency, overwrite=overwrite, complevel=complevel
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    def on_message(msg):
        print(msg)

    def gqa_predicate(ds):
        return ds.metadata.gqa_iterative_mean_xy <= gqa

    def collection_category_predicate(ds):
        if ds.type.name in ["ls5_sr", "ls7_sr", "ls8_sr", "ls9_sr"]:
            return ds.metadata.collection_category == usgs_collection_category
        else:
            return True

    predicate = None
    # These two are exclusive. GQA is from DEA, whereas collection_category is from USGS
    if gqa is not None:
        predicate = gqa_predicate
    if usgs_collection_category is not None:
        predicate = collection_category_predicate

    dc = Datacube(env=env)
    try:
        ok = tasks.save(
            dc,
            products,
            dataset_filter=filter,
            temporal_range=temporal_range,
            tiles=tiles,
            predicate=predicate,
            debug=debug,
            msg=on_message,
        )
    except ValueError as e:
        print(str(e))
        sys.exit(2)

    if not ok:
        # exit with error code, failure message was already printed
        sys.exit(3)
