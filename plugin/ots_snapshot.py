"""Read the marker state OpenTAKServer exposes from its current database."""

from __future__ import annotations

import json
import sys

import psycopg
import yaml


def main() -> int:
    if len(sys.argv) != 2:
        return 2

    try:
        with open(sys.argv[1], "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
        database_uri = str(config["SQLALCHEMY_DATABASE_URI"]).replace(
            "postgresql+psycopg://", "postgresql://", 1
        )

        with psycopg.connect(database_uri) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        marker.uid,
                        marker.callsign,
                        COALESCE(latest_cot.type, original_cot.type, 'a-u-G'),
                        point.latitude,
                        point.longitude,
                        point.hae,
                        point.ce,
                        point.le,
                        point.timestamp
                    FROM markers AS marker
                    JOIN points AS point ON point.id = marker.point_id
                    LEFT JOIN cot AS original_cot ON original_cot.id = marker.cot_id
                    LEFT JOIN LATERAL (
                        SELECT type
                        FROM cot
                        WHERE uid = marker.uid
                        ORDER BY timestamp DESC
                        LIMIT 1
                    ) AS latest_cot ON TRUE
                    WHERE point.latitude IS NOT NULL
                      AND point.longitude IS NOT NULL
                    ORDER BY point.timestamp DESC
                    """
                )
                markers = [
                    {
                        "uid": row[0],
                        "callsign": row[1],
                        "cot_type": row[2],
                        "latitude": row[3],
                        "longitude": row[4],
                        "hae": row[5],
                        "ce": row[6],
                        "le": row[7],
                        "time": row[8].isoformat(),
                    }
                    for row in cursor.fetchall()
                ]
        print(json.dumps({"markers": markers}))
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
