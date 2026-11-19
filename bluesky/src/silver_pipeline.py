"""Lakeflow pipeline: Bluesky bronze events -> current-state silver tables.

Each table is kept up to date with AUTO CDC keyed by (did, rkey) and ordered by
the repo revision, so deletions on Bluesky (deleted posts, unlikes, unreposts,
unfollows) remove the row here too.

Pipeline configuration:
    bluesky.bronze_table  fully qualified bronze table to read from
"""
from pyspark import pipelines as dp
from pyspark.sql import functions as F

BRONZE = spark.conf.get("bluesky.bronze_table")  # noqa: F821 (spark is provided by the pipeline)
CREATED_AT = "try_to_timestamp(event:record.createdAt::string)"
SUBJECT = {
    "subject_uri": "event:record.subject.uri::string",
    "subject_cid": "event:record.subject.cid::string",
    "created_at": CREATED_AT,
}

# table name -> (collection, comment, {column: expression over the VARIANT `event` payload})
TABLES = {
    "posts": ("app.bsky.feed.post", "Current Bluesky posts", {
        "cid": "event:cid::string",
        "text": "event:record.text::string",
        "langs": "try_cast(event:record.langs AS ARRAY<STRING>)",
        "reply_parent_uri": "event:record.reply.parent.uri::string",
        "reply_root_uri": "event:record.reply.root.uri::string",
        "embed_type": "event:record.embed['$type']::string",
        "created_at": CREATED_AT,
        "record": "event:record",
    }),
    "likes": ("app.bsky.feed.like", "Current Bluesky likes", SUBJECT),
    "reposts": ("app.bsky.feed.repost", "Current Bluesky reposts", SUBJECT),
    "follows": ("app.bsky.graph.follow", "Current Bluesky follows (did follows subject_did)", {
        "subject_did": "event:record.subject::string",
        "created_at": CREATED_AT,
    }),
}


def define_table(name, collection, comment, columns):
    @dp.temporary_view(name=f"{name}_changes")
    def changes():
        return (
            spark.readStream.table(BRONZE)  # noqa: F821
            .where(f"kind = 'commit' AND collection = '{collection}'")
            .withWatermark("event_time", "15 minutes")
            .dropDuplicatesWithinWatermark(["did", "rkey", "rev"])
            .selectExpr(
                "did",
                "rkey",
                f"concat('at://', did, '/{collection}/', rkey) AS uri",
                *[f"{expr} AS {col}" for col, expr in columns.items()],
                "operation",
                "rev",
                "event_time",
            )
        )

    dp.create_streaming_table(name, comment=comment)
    dp.create_auto_cdc_flow(
        target=name,
        source=f"{name}_changes",
        keys=["did", "rkey"],
        sequence_by="rev",
        apply_as_deletes=F.expr("operation = 'delete'"),
        except_column_list=["operation"],
        stored_as_scd_type=1,
    )


for table, spec in TABLES.items():
    define_table(table, *spec)
