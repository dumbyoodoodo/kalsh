{
  "banner": "OPERATIONAL DATA-QUALITY REVIEW ONLY \u2014 NO PERFORMANCE ANALYSIS",
  "not_ready_verdict": null,
  "gate_open": true,
  "as_of": "2026-08-04T00:00:00+00:00",
  "review_gate": "2026-08-04T00:00:00+00:00",
  "seconds_until_gate": 0.0,
  "overall_verdict": "PILOT_COLLECTION_EXTENSION_REQUIRED",
  "stations": [
    {
      "station": "SEA",
      "decision": "EXTEND_COLLECTION",
      "reasons": [
        "pilot window overlaps a collection gap not classified in the permanent gap ledger; attribution is unavailable",
        "evidence incomplete \u2014 cannot confirm clean for: parser_failures, source_unavailable_attempts"
      ]
    },
    {
      "station": "PHX",
      "decision": "EXTEND_COLLECTION",
      "reasons": [
        "pilot window overlaps a collection gap not classified in the permanent gap ledger; attribution is unavailable",
        "evidence incomplete \u2014 cannot confirm clean for: parser_failures, source_unavailable_attempts"
      ]
    },
    {
      "station": "MIA",
      "decision": "EXTEND_COLLECTION",
      "reasons": [
        "pilot window overlaps a collection gap not classified in the permanent gap ledger; attribution is unavailable",
        "evidence incomplete \u2014 cannot confirm clean for: parser_failures, source_unavailable_attempts"
      ]
    }
  ],
  "station_counts": [
    {
      "station": "SEA",
      "completeness": {
        "expected_dates": 7,
        "dates_with_tmax": 7,
        "dates_with_tmin": 7,
        "dates_with_both": 7,
        "missing_dates": [],
        "partial_current_excluded": true,
        "duplicate_observation_rows": 0,
        "backfill_only_dates": 0
      },
      "forecast_coverage": {
        "issuances": 17,
        "expected_issuances": 14,
        "longest_gap_hours": 24.0,
        "source_gap_windows": 2
      },
      "provenance": {
        "observations_missing_raw_payload": 0,
        "observations_missing_source_product": 0,
        "orphans": 0,
        "environment_inconsistencies": 0
      },
      "parser_source_health": {
        "parser_failures": 0,
        "malformed_products": 0,
        "source_unavailable_attempts": 0,
        "timezone_date_mismatches": 0
      },
      "operational": {
        "weather_gap_dates_outage_explained": 0,
        "weather_gap_dates_unexplained": 0
      },
      "evidence_unavailable": [
        "parser_failures",
        "source_unavailable_attempts"
      ]
    },
    {
      "station": "PHX",
      "completeness": {
        "expected_dates": 7,
        "dates_with_tmax": 7,
        "dates_with_tmin": 7,
        "dates_with_both": 7,
        "missing_dates": [],
        "partial_current_excluded": true,
        "duplicate_observation_rows": 0,
        "backfill_only_dates": 0
      },
      "forecast_coverage": {
        "issuances": 42,
        "expected_issuances": 14,
        "longest_gap_hours": 24.0,
        "source_gap_windows": 2
      },
      "provenance": {
        "observations_missing_raw_payload": 0,
        "observations_missing_source_product": 0,
        "orphans": 0,
        "environment_inconsistencies": 0
      },
      "parser_source_health": {
        "parser_failures": 0,
        "malformed_products": 0,
        "source_unavailable_attempts": 0,
        "timezone_date_mismatches": 0
      },
      "operational": {
        "weather_gap_dates_outage_explained": 0,
        "weather_gap_dates_unexplained": 0
      },
      "evidence_unavailable": [
        "parser_failures",
        "source_unavailable_attempts"
      ]
    },
    {
      "station": "MIA",
      "completeness": {
        "expected_dates": 7,
        "dates_with_tmax": 7,
        "dates_with_tmin": 7,
        "dates_with_both": 7,
        "missing_dates": [],
        "partial_current_excluded": true,
        "duplicate_observation_rows": 0,
        "backfill_only_dates": 0
      },
      "forecast_coverage": {
        "issuances": 30,
        "expected_issuances": 14,
        "longest_gap_hours": 22.23,
        "source_gap_windows": 2
      },
      "provenance": {
        "observations_missing_raw_payload": 0,
        "observations_missing_source_product": 0,
        "orphans": 0,
        "environment_inconsistencies": 0
      },
      "parser_source_health": {
        "parser_failures": 0,
        "malformed_products": 0,
        "source_unavailable_attempts": 0,
        "timezone_date_mismatches": 0
      },
      "operational": {
        "weather_gap_dates_outage_explained": 0,
        "weather_gap_dates_unexplained": 0
      },
      "evidence_unavailable": [
        "parser_failures",
        "source_unavailable_attempts"
      ]
    }
  ],
  "reasons": [
    "SEA: EXTEND_COLLECTION \u2014 pilot window overlaps a collection gap not classified in the permanent gap ledger; attribution is unavailable; evidence incomplete \u2014 cannot confirm clean for: parser_failures, source_unavailable_attempts",
    "PHX: EXTEND_COLLECTION \u2014 pilot window overlaps a collection gap not classified in the permanent gap ledger; attribution is unavailable; evidence incomplete \u2014 cannot confirm clean for: parser_failures, source_unavailable_attempts",
    "MIA: EXTEND_COLLECTION \u2014 pilot window overlaps a collection gap not classified in the permanent gap ledger; attribution is unavailable; evidence incomplete \u2014 cannot confirm clean for: parser_failures, source_unavailable_attempts"
  ],
  "gap_ledger": {
    "valid": true,
    "unclassified_gap_overlap": true,
    "annotations": [
      {
        "gap_id": "GAP-20260717-LAX-CLI-RECORD-FLAG",
        "classification": "PARSER_FAILURE",
        "subsystems": [
          "WEATHER_OBSERVATIONS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "RECOVERED_NOT_CONTEMPORANEOUS",
        "start_at": "2026-07-18T01:28:00+00:00",
        "end_at": "2026-07-28T16:12:45+00:00",
        "evidence_refs": [
          "weather_observations.id in (26813,26814,26815,26816)",
          "raw_api_payloads.id in (752042,752043)",
          "202607180128-KLOX-CDUS46-CLILAX",
          "git:1662ef8:Parse record-flagged CLI temperature values",
          "docs/runbooks/data_quality_exceptions.md"
        ]
      },
      {
        "gap_id": "GAP-20260716-MIA-CLI-RECORD-FLAG",
        "classification": "PARSER_FAILURE",
        "subsystems": [
          "WEATHER_OBSERVATIONS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "RECOVERED_NOT_CONTEMPORANEOUS",
        "start_at": "2026-07-16T20:24:00+00:00",
        "end_at": "2026-07-28T16:12:56+00:00",
        "evidence_refs": [
          "weather_observations.id in (26817,26818,26819,26820)",
          "raw_api_payloads.id in (644916,644918)",
          "202607162024-KMFL-CDUS42-CLIMIA",
          "git:1662ef8:Parse record-flagged CLI temperature values",
          "docs/runbooks/data_quality_exceptions.md"
        ]
      },
      {
        "gap_id": "GAP-20260702-NYC-CLI-RECORD-FLAG",
        "classification": "PARSER_FAILURE",
        "subsystems": [
          "WEATHER_OBSERVATIONS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "RECOVERED_NOT_CONTEMPORANEOUS",
        "start_at": "2026-07-02T20:38:00+00:00",
        "end_at": "2026-07-28T16:12:58+00:00",
        "evidence_refs": [
          "weather_observations.id in (26821,26822,26823,26824)",
          "raw_api_payloads.id in (48,50)",
          "202607022038-KOKX-CDUS41-CLINYC",
          "git:1662ef8:Parse record-flagged CLI temperature values",
          "docs/runbooks/data_quality_exceptions.md"
        ]
      },
      {
        "gap_id": "GAP-20260724-PHX-CLI-RECORD-FLAG",
        "classification": "PARSER_FAILURE",
        "subsystems": [
          "WEATHER_OBSERVATIONS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "RECOVERED_NOT_CONTEMPORANEOUS",
        "start_at": "2026-07-25T00:29:00+00:00",
        "end_at": "2026-07-28T16:12:59+00:00",
        "evidence_refs": [
          "weather_observations.id in (26825,26826,26827,26828)",
          "raw_api_payloads.id in (644940,644941)",
          "202607250029-KPSR-CDUS45-CLIPHX",
          "git:1662ef8:Parse record-flagged CLI temperature values",
          "docs/runbooks/data_quality_exceptions.md"
        ]
      },
      {
        "gap_id": "GAP-CLI-RECORD-FLAG-DEFECT",
        "classification": "PARSER_FAILURE",
        "subsystems": [
          "WEATHER_OBSERVATIONS"
        ],
        "confidence": "HIGH",
        "research_treatment": "RECOVERED_NOT_CONTEMPORANEOUS",
        "start_at": "2026-07-02T20:38:00+00:00",
        "end_at": "2026-07-28T16:12:59+00:00",
        "evidence_refs": [
          "git:1662ef8:Parse record-flagged CLI temperature values",
          "weather_observations.id between 26813 and 26828",
          "docs/runbooks/data_quality_exceptions.md"
        ]
      },
      {
        "gap_id": "GAP-20260730-2223-HOST-SLEEP-IV1",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-07-30T22:23:40+00:00",
        "end_at": "2026-07-31T02:25:49+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-07-30T22:23:40Z..2026-07-31T02:25:49Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)"
        ]
      },
      {
        "gap_id": "GAP-20260731-0225-HOST-SLEEP-IV2",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "HIGH",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-07-31T02:25:49+00:00",
        "end_at": "2026-07-31T15:07:26+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-07-31T02:25:49Z..2026-07-31T15:07:26Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)"
        ]
      },
      {
        "gap_id": "GAP-20260731-1608-HOST-SLEEP-IV3",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-07-31T16:08:28+00:00",
        "end_at": "2026-07-31T20:24:13+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-07-31T16:08:28Z..2026-07-31T20:24:13Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)",
          "collector_runs: weather run started 2026-07-31T15:07:26Z, duration_seconds=3662.2"
        ]
      },
      {
        "gap_id": "GAP-20260731-2054-HOST-SLEEP-IV4",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-07-31T20:54:31+00:00",
        "end_at": "2026-07-31T23:04:41+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-07-31T20:54:31Z..2026-07-31T23:04:41Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)"
        ]
      },
      {
        "gap_id": "GAP-20260731-2304-HOST-SLEEP-IV5",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "HIGH",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-07-31T23:04:41+00:00",
        "end_at": "2026-08-01T19:01:42+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-07-31T23:04:41Z..2026-08-01T19:01:42Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)"
        ]
      },
      {
        "gap_id": "GAP-20260801-2242-HOST-SLEEP-IV6",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-08-01T22:42:40+00:00",
        "end_at": "2026-08-02T03:03:37+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-08-01T22:42:40Z..2026-08-02T03:03:37Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)"
        ]
      },
      {
        "gap_id": "GAP-20260802-1738-HOST-SLEEP-IV7",
        "classification": "HOST_UNAVAILABLE",
        "subsystems": [
          "WEATHER_OBSERVATIONS",
          "WEATHER_FORECASTS"
        ],
        "confidence": "CONFIRMED",
        "research_treatment": "EXCLUDE_FOR_MISSING_COLLECTION",
        "start_at": "2026-08-02T17:38:07+00:00",
        "end_at": "2026-08-02T20:20:26+00:00",
        "evidence_refs": [
          "pmset -g log: Sleep events 2026-08-02T17:38:07Z..2026-08-02T20:20:26Z",
          "collector_runs: 0 weather runs in interval; kalshi/prices runs present",
          "collector.out.log*: no ops.run.starting/stopped inside interval",
          "last reboot / wtmp (from 2025-12-30)"
        ]
      }
    ],
    "usage": "read-only annotation source; this review never appends, amends, or reclassifies a gap record, and never treats a recovered-not-contemporaneous row as contemporaneously collected"
  },
  "forbidden_analysis": "no forecast error / calibration / expected or realized value / spread / liquidity / price / trade outcome / profitability / station ranking / model comparison is computed by this review",
  "operator_approval_required": "any station-registry change (keep-as-is, extend, or removal) requires a separate explicit human approval; this review mutates nothing",
  "collector_gap_attribution": {
    "unclassified_overlap": true,
    "total_uncovered_hours": 1.018,
    "candidate_gaps": 7,
    "uncovered_or_ambiguous_gaps": 5,
    "coverages": [
      {
        "gap": {
          "start_at": "2026-07-30T22:23:40.837462+00:00",
          "end_at": "2026-07-31T02:25:49.627509+00:00",
          "duration_hours": 4.036,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-07-30T22:23:40.837462+00:00 and 2026-07-31T02:25:49.627509+00:00"
          ]
        },
        "status": "COVERED_CONFIRMED",
        "covering_gap_ids": [
          "GAP-20260730-2223-HOST-SLEEP-IV1",
          "GAP-20260731-0225-HOST-SLEEP-IV2"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-07-30T22:23:40.837462+00:00",
            "end_at": "2026-07-31T02:25:49.627509+00:00"
          }
        ],
        "uncovered_intervals": [],
        "uncovered_hours": 0,
        "unclassified": false
      },
      {
        "gap": {
          "start_at": "2026-07-31T02:25:49.627509+00:00",
          "end_at": "2026-07-31T15:07:26.631676+00:00",
          "duration_hours": 12.694,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-07-31T02:25:49.627509+00:00 and 2026-07-31T15:07:26.631676+00:00"
          ]
        },
        "status": "COVERED_PARTIAL",
        "covering_gap_ids": [
          "GAP-20260731-0225-HOST-SLEEP-IV2"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-07-31T02:25:49.627509+00:00",
            "end_at": "2026-07-31T15:07:26+00:00"
          }
        ],
        "uncovered_intervals": [
          {
            "start_at": "2026-07-31T15:07:26+00:00",
            "end_at": "2026-07-31T15:07:26.631676+00:00"
          }
        ],
        "uncovered_hours": 0.0,
        "unclassified": true
      },
      {
        "gap": {
          "start_at": "2026-07-31T15:07:26.631676+00:00",
          "end_at": "2026-07-31T20:24:13.801593+00:00",
          "duration_hours": 5.28,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-07-31T15:07:26.631676+00:00 and 2026-07-31T20:24:13.801593+00:00"
          ]
        },
        "status": "COVERED_PARTIAL",
        "covering_gap_ids": [
          "GAP-20260731-1608-HOST-SLEEP-IV3"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-07-31T16:08:28+00:00",
            "end_at": "2026-07-31T20:24:13+00:00"
          }
        ],
        "uncovered_intervals": [
          {
            "start_at": "2026-07-31T15:07:26.631676+00:00",
            "end_at": "2026-07-31T16:08:28+00:00"
          },
          {
            "start_at": "2026-07-31T20:24:13+00:00",
            "end_at": "2026-07-31T20:24:13.801593+00:00"
          }
        ],
        "uncovered_hours": 1.017,
        "unclassified": true
      },
      {
        "gap": {
          "start_at": "2026-07-31T20:54:31.718377+00:00",
          "end_at": "2026-07-31T23:04:41.165138+00:00",
          "duration_hours": 2.169,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-07-31T20:54:31.718377+00:00 and 2026-07-31T23:04:41.165138+00:00"
          ]
        },
        "status": "COVERED_CONFIRMED",
        "covering_gap_ids": [
          "GAP-20260731-2054-HOST-SLEEP-IV4",
          "GAP-20260731-2304-HOST-SLEEP-IV5"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-07-31T20:54:31.718377+00:00",
            "end_at": "2026-07-31T23:04:41.165138+00:00"
          }
        ],
        "uncovered_intervals": [],
        "uncovered_hours": 0,
        "unclassified": false
      },
      {
        "gap": {
          "start_at": "2026-07-31T23:04:41.165138+00:00",
          "end_at": "2026-08-01T19:01:42.224009+00:00",
          "duration_hours": 19.95,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-07-31T23:04:41.165138+00:00 and 2026-08-01T19:01:42.224009+00:00"
          ]
        },
        "status": "COVERED_PARTIAL",
        "covering_gap_ids": [
          "GAP-20260731-2304-HOST-SLEEP-IV5"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-07-31T23:04:41.165138+00:00",
            "end_at": "2026-08-01T19:01:42+00:00"
          }
        ],
        "uncovered_intervals": [
          {
            "start_at": "2026-08-01T19:01:42+00:00",
            "end_at": "2026-08-01T19:01:42.224009+00:00"
          }
        ],
        "uncovered_hours": 0.0,
        "unclassified": true
      },
      {
        "gap": {
          "start_at": "2026-08-01T22:42:40.475066+00:00",
          "end_at": "2026-08-02T03:03:37.105623+00:00",
          "duration_hours": 4.349,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-08-01T22:42:40.475066+00:00 and 2026-08-02T03:03:37.105623+00:00"
          ]
        },
        "status": "COVERED_PARTIAL",
        "covering_gap_ids": [
          "GAP-20260801-2242-HOST-SLEEP-IV6"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-08-01T22:42:40.475066+00:00",
            "end_at": "2026-08-02T03:03:37+00:00"
          }
        ],
        "uncovered_intervals": [
          {
            "start_at": "2026-08-02T03:03:37+00:00",
            "end_at": "2026-08-02T03:03:37.105623+00:00"
          }
        ],
        "uncovered_hours": 0.0,
        "unclassified": true
      },
      {
        "gap": {
          "start_at": "2026-08-02T17:38:07.614711+00:00",
          "end_at": "2026-08-02T20:20:26.404432+00:00",
          "duration_hours": 2.705,
          "subsystem": "WEATHER_OBSERVATIONS",
          "kind": "no_runs",
          "evidence_refs": [
            "collector_runs:weather:no run between 2026-08-02T17:38:07.614711+00:00 and 2026-08-02T20:20:26.404432+00:00"
          ]
        },
        "status": "COVERED_PARTIAL",
        "covering_gap_ids": [
          "GAP-20260802-1738-HOST-SLEEP-IV7"
        ],
        "covered_intervals": [
          {
            "start_at": "2026-08-02T17:38:07.614711+00:00",
            "end_at": "2026-08-02T20:20:26+00:00"
          }
        ],
        "uncovered_intervals": [
          {
            "start_at": "2026-08-02T20:20:26+00:00",
            "end_at": "2026-08-02T20:20:26.404432+00:00"
          }
        ],
        "uncovered_hours": 0.0,
        "unclassified": true
      }
    ],
    "affected_local_dates_by_station": {
      "SEA": [
        "2026-07-31",
        "2026-08-01",
        "2026-08-02"
      ],
      "PHX": [
        "2026-07-31",
        "2026-08-01",
        "2026-08-02"
      ],
      "MIA": [
        "2026-07-31",
        "2026-08-01",
        "2026-08-02"
      ]
    },
    "note": "derived from collector-run continuity minus active permanent-gap-ledger coverage; ledger confidence alone never implies attribution is complete"
  }
}
