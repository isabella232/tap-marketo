import csv
import io
import json
import pendulum
import singer
from singer import bookmarks

from tap_marketo.client import ExportFailed


# We can request up to 30 days worth of activities per export.
MAX_EXPORT_DAYS = 30

BASE_ACTIVITY_FIELDS = [
    "marketoGUID",
    "leadId",
    "activityDate",
    "activityTypeId",
]

ACTIVITY_FIELDS = BASE_ACTIVITY_FIELDS + [
    "primaryAttributeValue",
    "primaryAttributeValueId",
    "attributes",
]

NO_ASSET_MSG = "No assets found for the given search criteria."
NO_CORONA_WARNING = (
    "Your account does not have Corona support enabled. Without Corona, each sync of "
    "the Leads table requires a full export which can lead to lower data freshness. "
    "Please contact <contact email> at Marketo to request Corona support be added to "
    "your account."
)


def format_value(value, schema):
    if not isinstance(schema["type"], list):
        field_type = [schema["type"]]
    else:
        field_type = schema["type"]

    if value in [None, ""]:
        return None
    elif schema.get("format") == "date-time":
        return pendulum.parse(value).isoformat()
    elif "integer" in field_type:
        return int(value)
    elif "number" in field_type:
        return float(value)
    elif "boolean" in field_type:
        if isinstance(value, bool):
            return value
        return value.lower() == "true"

    return value


def format_values(stream, row):
    rtn = {}
    for field, schema in stream["schema"]["properties"].items():
        if not schema.get("selected"):
            continue
        rtn[field] = format_value(row.get(field), schema)
    return rtn


def parse_csv_line(line):
    reader = csv.reader(io.StringIO(line.decode('utf-8')))
    return next(reader)


def flatten_activity(row, schema):
    # Start with the base fields
    rtn = {field: row[field] for field in BASE_ACTIVITY_FIELDS}

    # Move the primary attribute to the named column. Primary attribute
    # has a `field` and a `field_id` entry in the schema, is marked for
    # automatic inclusion, and isn't one of the base activity fields.
    # TODO: metadata this
    for field, field_schema in schema["properties"].items():
        if field_schema["inclusion"] == "automatic" and field not in ACTIVITY_FIELDS:
            rtn[field] = row["primaryAttributeValue"]

    # Now flatten the attrs json to it's selected columns
    if "attributes" in row:
        attrs = json.loads(row["attributes"])
        for key, value in attrs.items():
            key = key.lower().replace(" ", "_")
            rtn[key] = value

    return rtn


def get_or_create_export(client, state, stream):
    export_id = bookmarks.get_bookmark(state, stream["stream"], "export_id")
    if not export_id:
        # Stream names for activities are `activities_X` where X is the
        # activity type id in Marketo. We need the activity type id to
        # build the query.
        _, activity_type_id = stream["stream"].split("_")

        # Activities must be queried by `createdAt` even though
        # that is not a real field. `createdAt` proxies `activityDate`.
        # The activity type id must also be included in the query. The
        # largest date range that can be used for activities is 30 days.
        start_date = bookmarks.get_bookmark(state, stream["stream"], stream["replication_key"])
        end_pen = start_pen.add(days=MAX_EXPORT_DAYS)
        if end_pen >= pendulum.utcnow():
            end_pen = pendulum.utcnow()

        end_date = end_pen.isoformat()
        query = {"createdAt": {"startsAt": start_date, "endsAt": end_date},
                 "activityTypeIds": [activity_type_id]}

        # Create the new export and store the id and end date in state.
        # Does not start the export (must POST to the "enqueue" endpoint).
        export_id = client.create_export("activities", ACTIVITY_FIELDS, query)
        update_activity_state(state, stream, export_id=export_id, export_end=end_date)

    return export_id


def update_activity_state(state, stream, bookmark=None, export_id=None, export_end=None):
    state = bookmarks.write_bookmark(state, stream["stream"], "export_id", export_id)
    state = bookmarks.write_bookmark(state, stream["stream"], "export_end", export_end)
    if bookmark:
        state = bookmarks.write_bookmark(state, stream["stream"], stream["replication_key"], bookmark)

    singer.write_state(state)
    return state


def handle_activity_line(state, stream, headers, line):
    parsed_line = parse_csv_line(line)
    row = dict(zip(headers, parsed_line))
    row = flatten_activity(row, stream["schema"])
    record = format_values(stream, row)

    start_date = bookmarks.get_bookmark(state, stream["stream"], stream["replication_key"])
    if record[stream["replication_key"]] < start_date:
        return 0

    singer.write_record(stream["stream"], record)
    return 1


def sync_activities(client, state, stream):
    start_date = bookmarks.get_bookmark(state, stream["stream"], stream["replication_key"])
    start_pen = pendulum.parse(start_date)
    job_started = pendulum.utcnow()
    record_count = 0

    while start_pen < job_started:
        export_id = get_or_create_export(client, state, stream)

        # If the export fails while running, clear the export information
        # from state so a new export can be run next sync.
        try:
            client.wait_for_export("activities", export_id)
        except ExportFailed:
            update_activity_state(state, stream)
            raise

        # Stream the rows keeping count of the accepted rows.
        lines = client.stream_export("activities", export_id)
        headers = parse_csv_line(next(lines))
        for line in lines:
            record_count += handle_activity_line(state, stream, headers, line)

        # The new start date is the end of the previous export. Update
        # the bookmark to the end date and continue with the next export.
        start_date = bookmarks.get_bookmark(state, stream["stream"], "export_end")
        update_activity_state(state, stream, bookmark=start_date)
        start_pen = pendulum.parse(start_date)

    return state, record_count


def sync_programs(client, state, stream):
    # Programs are queryable via their updatedAt time but require and
    # end date as well. As there is no max time range for the query,
    # query from the bookmark value until current.
    #
    # The Programs endpoint uses offsets with a return limit of 200
    # per page. If requesting past the final program, an error message
    # is returned to indicate that the endpoint has been fully synced.
    start_date = bookmarks.get_bookmark(state, "programs", "updatedAt")
    end_date = pendulum.utcnow().isoformat()
    params = {
        "maxReturn": 200,
        "offset": 0,
        "earliestUpdatedAt": start_date,
        "latestUpdatedAt": end_date,
    }
    endpoint = "rest/asset/v1/programs.json"

    record_count = 0
    while True:
        data = client.request("GET", endpoint, endpoint_name="programs", params=params)

        # If the no asset message is in the warnings, we have exhausted
        # the search results and can end the sync.
        if NO_ASSET_MSG in data["warnings"]:
            break

        # Each row just needs the values formatted. If the record is
        # newer than the original start date, stream the record.
        for row in data["result"]:
            record = format_values(stream, row)
            if record["updatedAt"] >= start_date:
                record_count += 1
                singer.write_record("programs", record)

        # Increment the offset by the return limit for the next query.
        params["offset"] += params["maxReturn"]

    # Now that we've finished every page we can update the bookmark to
    # the end of the query.
    state = bookmarks.write_bookmark(state, "programs", "updatedAt", end_date)
    singer.write_state(state)
    return state, record_count


def sync_paginated(client, state, stream):
    # Campaigns and Static Lists are paginated with a max return of 300
    # items per page. There are no filters that can be used to only
    # return updated records.
    start_date = bookmarks.get_bookmark(state, stream["stream"], stream["replication_key"])
    params = {"batchSize": 300}
    endpoint = "rest/v1/{}.json".format(stream["stream"])

    # Paginated requests use paging tokens for retrieving the next page
    # of results. These tokens are stored in the state for resuming
    # syncs. If a paging token exists in state, use it.
    next_page_token = bookmarks.get_bookmark(state, stream["stream"], "next_page_token")
    if next_page_token:
        params["nextPageToken"] = next_page_token

    # Keep querying pages of data until no next page token.
    record_count = 0
    max_bookmark = start_date
    while True:
        data = client.request("GET", endpoint, endpoint_name=stream["stream"], params=params)

        # Each row just needs the values formatted. If the record is
        # newer than the original start date, stream the record. Finally,
        # update the bookmark if newer than the existing bookmark.
        for row in data["result"]:
            record = format_values(stream, row)
            if record[stream["replication_key"]] >= start_date:
                record_count += 1
                singer.write_record(stream["stream"], record)
                bookmark = bookmarks.get_bookmark(state, stream["stream"], stream["replication_key"])
                if bookmark > max_bookmark:
                    max_bookmark = bookmark

        # No next page, results are exhausted.
        if "nextPageToken" not in data:
            break

        # Store the next page token in state and continue.
        params["nextPageToken"] = data["nextPageToken"]
        state = bookmarks.write_bookmark(state, stream["stream"], "next_page_token", data["nextPageToken"])
        singer.write_state(state)

    # Once all results are exhausted, unset the next page token bookmark
    # so the subsequent sync starts from the beginning.
    state = bookmarks.write_bookmark(state, stream["stream"], "next_page_token", None)
    state = bookmarks.write_bookmark(state, stream["stream"], stream["replication_key"], max_bookmark)
    singer.write_state(state)
    return state, record_count


def sync_activity_types(client, state, stream):
    # Activity types aren't even paginated. Grab all the results in one
    # request, format the values, and output them.
    endpoint = "rest/v1/activities/types.json"
    data = client.request("GET", endpoint, endpoint_name="activity_types")
    record_count = 0
    for row in data["result"]:
        record = format_values(stream, row)
        record_count += 1
        singer.write_record("activity_types", record)

    return state, record_count


def sync(client, catalog, state):
    starting_stream = bookmarks.get_currently_syncing(state)
    if starting_stream:
        singer.log_info("Resuming sync from %s", starting_stream)
    else:
        singer.log_info("Starting sync")

    for stream in catalog["streams"]:
        # Skip unselected streams.
        if not stream["schema"].get("selected"):
            singer.log_info("%s: not selected", stream["stream"])
            continue

        # Skip streams that have already be synced when resuming.
        if starting_stream and stream["stream"] != starting_stream:
            singer.log_info("%s: already synced", stream["stream"])
            continue

        singer.log_info("%s: starting sync", stream["stream"])

        # Now that we've started, there's no more "starting stream". Set
        # the current stream to resume on next run.
        starting_stream = None
        state = bookmarks.set_currently_syncing(state, stream["stream"])
        singer.write_state(state)

        # Sync stream based on type.
        if stream["stream"] == "activity_types":
            state, record_count = sync_activity_types(client, state, stream)
        elif stream["stream"].startswith("activities_"):
            state, record_count = sync_activities(client, state, stream)
        elif stream["stream"] in ["campaigns", "lists"]:
            state, record_count = sync_paginated(client, state, stream)
        elif stream["stream"] == "programs":
            state, record_count = sync_programs(client, state, stream)
        else:
            raise Exception("Stream %s not implemented" % stream["stream"])

        # Emit metric for record count.
        counter = singer.metrics.record_counter(stream["stream"])
        counter.value = record_count
        counter._pop()

        # Unset current stream.
        state = bookmarks.set_currently_syncing(state, None)
        singer.write_state(state)
        singer.log_info("%s: finished sync", stream["stream"])

    singer.log_info("Finished sync")

    # If Corona is not supported, log a warning near the end of the tap
    # log with instructions on how to get Corona supported.
    if not client.use_corona:
        singer.log_warning(NO_CORONA_WARNING)
