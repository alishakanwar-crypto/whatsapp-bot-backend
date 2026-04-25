"""Embedded seed data for agent DVRs and camera mappings.

This data is used by init_db() to auto-seed the cloud DB when
the agent_dvrs table is empty (e.g. after a Fly.io restart that
wipes the ephemeral SQLite database).
"""

import os

_DVR_PASSWORD = os.getenv("DVR_DEFAULT_PASSWORD", "")
if not _DVR_PASSWORD:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "DVR_DEFAULT_PASSWORD env var not set — DVR seed entries will have empty passwords"
    )

SEED_DVRS = [
    {
        "name": "DVR 1",
        "ip": "192.168.0.11",
        "port": 80,
        "username": "admin",
        "password": _DVR_PASSWORD,
        "channels": 64
    },
    {
        "name": "DVR 2",
        "ip": "192.168.0.12",
        "port": 80,
        "username": "admin",
        "password": _DVR_PASSWORD,
        "channels": 64
    },
    {
        "name": "DVR 3",
        "ip": "192.168.0.14",
        "port": 80,
        "username": "admin",
        "password": _DVR_PASSWORD,
        "channels": 64
    }
]

SEED_CAMERA_MAPPING = {
    "GRADE 12B": {
        "dvr_index": 0,
        "channel": 1,
        "description": "G12B C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 1,
                "description": "G12B C2"
            },
            {
                "dvr_index": 0,
                "channel": 16,
                "description": "G12B C1"
            }
        ]
    },
    "GRADE 9A": {
        "dvr_index": 0,
        "channel": 2,
        "description": "G9A C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 2,
                "description": "G9A C2"
            },
            {
                "dvr_index": 0,
                "channel": 20,
                "description": "G9A C1"
            }
        ]
    },
    "GRADE 9B": {
        "dvr_index": 0,
        "channel": 3,
        "description": "G9B C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 3,
                "description": "G9B C1"
            },
            {
                "dvr_index": 0,
                "channel": 21,
                "description": "G9B C2"
            }
        ]
    },
    "GALLERY LIB 1": {
        "dvr_index": 0,
        "channel": 4,
        "description": "F Floor Library Gal  C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 4,
                "description": "F Floor Library Gal  C1"
            },
            {
                "dvr_index": 1,
                "channel": 15,
                "description": "G Floor L/W Middel Str"
            }
        ]
    },
    "GRADE 11A": {
        "dvr_index": 0,
        "channel": 5,
        "description": "G11A C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 5,
                "description": "G11A C2"
            },
            {
                "dvr_index": 0,
                "channel": 14,
                "description": "G11A C2"
            }
        ]
    },
    "COMPUTER LAB": {
        "dvr_index": 0,
        "channel": 6,
        "description": "COM LAB C1",
        "cam_type": ""
    },
    "GRADE 10A": {
        "dvr_index": 0,
        "channel": 7,
        "description": "G10A C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 7,
                "description": "G10A C1"
            },
            {
                "dvr_index": 0,
                "channel": 23,
                "description": "G10A C2"
            }
        ]
    },
    "GRADE 10B": {
        "dvr_index": 0,
        "channel": 8,
        "description": "G10B C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 8,
                "description": "G10B C1"
            },
            {
                "dvr_index": 0,
                "channel": 9,
                "description": "G10B C2"
            }
        ]
    },
    "GRADE 11B": {
        "dvr_index": 0,
        "channel": 10,
        "description": "G11B C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 10,
                "description": "G11B C1"
            },
            {
                "dvr_index": 0,
                "channel": 11,
                "description": "G11B C2"
            }
        ]
    },
    "MATH LAB 2": {
        "dvr_index": 0,
        "channel": 12,
        "description": "MATH LAB C2",
        "cam_type": ""
    },
    "GALLERY MID 2": {
        "dvr_index": 0,
        "channel": 13,
        "description": "F Floor R/W Gall C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 13,
                "description": "F Floor R/W Gall C2"
            },
            {
                "dvr_index": 2,
                "channel": 4,
                "description": "SECOND FLOOR ART GALLERY"
            }
        ]
    },
    "COMPUTER LAB 2": {
        "dvr_index": 0,
        "channel": 15,
        "description": "COM LAB C2",
        "cam_type": ""
    },
    "GALLERY MID": {
        "dvr_index": 0,
        "channel": 17,
        "description": "F Floor R/W Middel Str",
        "cam_type": ""
    },
    "GRADE 9C": {
        "dvr_index": 0,
        "channel": 18,
        "description": "G9C C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 18,
                "description": "G9C C1"
            },
            {
                "dvr_index": 0,
                "channel": 19,
                "description": "G9C C2"
            }
        ]
    },
    "MATH LAB 1": {
        "dvr_index": 0,
        "channel": 22,
        "description": "MATH LAB C1",
        "cam_type": ""
    },
    "GALLERY MID 3": {
        "dvr_index": 0,
        "channel": 24,
        "description": "F Floor R/W Gall C1",
        "cam_type": ""
    },
    "GALLERY MID 4": {
        "dvr_index": 0,
        "channel": 25,
        "description": "F Floor L/W First Str",
        "cam_type": ""
    },
    "GRADE 6B": {
        "dvr_index": 0,
        "channel": 26,
        "description": "G6B C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 26,
                "description": "G6B C2"
            },
            {
                "dvr_index": 0,
                "channel": 36,
                "description": "G6B C1"
            }
        ]
    },
    "SCIENCE LAB 2": {
        "dvr_index": 0,
        "channel": 27,
        "description": "SCI LAB C2",
        "cam_type": ""
    },
    "GRADE 6A": {
        "dvr_index": 0,
        "channel": 28,
        "description": "G6A C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 28,
                "description": "G6A C2"
            },
            {
                "dvr_index": 0,
                "channel": 46,
                "description": "G6A C1"
            }
        ]
    },
    "GRADE 4B": {
        "dvr_index": 0,
        "channel": 29,
        "description": "G4B C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 29,
                "description": "G4B C2"
            },
            {
                "dvr_index": 0,
                "channel": 31,
                "description": "G4B C1"
            }
        ]
    },
    "GRADE 7B": {
        "dvr_index": 0,
        "channel": 30,
        "description": "G7B C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 30,
                "description": "G7B C1"
            },
            {
                "dvr_index": 0,
                "channel": 44,
                "description": "G7B C2"
            }
        ]
    },
    "GRADE 4A": {
        "dvr_index": 0,
        "channel": 32,
        "description": "G4A C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 32,
                "description": "G4A C2"
            },
            {
                "dvr_index": 0,
                "channel": 40,
                "description": "G4A C1"
            }
        ]
    },
    "GALLERY MID 5": {
        "dvr_index": 0,
        "channel": 33,
        "description": "F Floor L/W Middel Str",
        "cam_type": ""
    },
    "GALLERY MID 6": {
        "dvr_index": 0,
        "channel": 34,
        "description": "F Floor L/W Gall C2",
        "cam_type": ""
    },
    "TEACHER STAFF 1": {
        "dvr_index": 0,
        "channel": 35,
        "description": "TEC.STAFF C1",
        "cam_type": ""
    },
    "GRADE 5A": {
        "dvr_index": 0,
        "channel": 37,
        "description": "G5A C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 37,
                "description": "G5A C1"
            },
            {
                "dvr_index": 0,
                "channel": 42,
                "description": "G5A C2"
            }
        ]
    },
    "TEACHER STAFF 2": {
        "dvr_index": 0,
        "channel": 38,
        "description": "TEC.STAFF C2",
        "cam_type": ""
    },
    "GRADE 7A": {
        "dvr_index": 0,
        "channel": 39,
        "description": "G7A C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 39,
                "description": "G7A C1"
            },
            {
                "dvr_index": 0,
                "channel": 41,
                "description": "G7A C2"
            }
        ]
    },
    "GRADE 5B": {
        "dvr_index": 0,
        "channel": 43,
        "description": "G5B C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 43,
                "description": "G5B C1"
            },
            {
                "dvr_index": 0,
                "channel": 47,
                "description": "G5B C2"
            }
        ]
    },
    "SCIENCE LAB 1": {
        "dvr_index": 0,
        "channel": 45,
        "description": "SCI LAB C1",
        "cam_type": ""
    },
    "GALLERY LIB 2": {
        "dvr_index": 0,
        "channel": 48,
        "description": "F Floor Library Gal C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 0,
                "channel": 48,
                "description": "F Floor Library Gal C2"
            },
            {
                "dvr_index": 1,
                "channel": 2,
                "description": "G FLOOR RECEPTION BACK GELLERY 2"
            }
        ]
    },
    "EDUCOMP ROOM": {
        "dvr_index": 0,
        "channel": 50,
        "description": "EDUCOMP ROOM",
        "cam_type": ""
    },
    "Academic Coordinator": {
        "dvr_index": 0,
        "channel": 51,
        "description": "Academic Coordinator",
        "cam_type": ""
    },
    "LIBRARY LAB 2": {
        "dvr_index": 0,
        "channel": 52,
        "description": "LIB ROOM C2",
        "cam_type": ""
    },
    "LIBRARY LAB 1": {
        "dvr_index": 0,
        "channel": 53,
        "description": "LIB ROOM C1",
        "cam_type": ""
    },
    "MINI COMPUTER LAB": {
        "dvr_index": 1,
        "channel": 50,
        "description": "MINI COMPUTER LAB",
        "cam_type": ""
    },
    "GRADE 1B": {
        "dvr_index": 1,
        "channel": 3,
        "description": "G1B C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 3,
                "description": "G1B C2"
            },
            {
                "dvr_index": 1,
                "channel": 7,
                "description": "G1B C1"
            }
        ]
    },
    "GRADE 2A": {
        "dvr_index": 1,
        "channel": 4,
        "description": "G2A  C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 4,
                "description": "G2A  C1"
            },
            {
                "dvr_index": 1,
                "channel": 6,
                "description": "G2A  C2"
            }
        ]
    },
    "GRADE 1A": {
        "dvr_index": 1,
        "channel": 5,
        "description": "G1A  C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 5,
                "description": "G1A  C1"
            },
            {
                "dvr_index": 1,
                "channel": 12,
                "description": "G1A C2"
            }
        ]
    },
    "DISPERSAL EXIT": {
        "dvr_index": 1,
        "channel": 8,
        "description": "G Floor DISPERSAL EXIT",
        "cam_type": ""
    },
    "GRADE 2B": {
        "dvr_index": 1,
        "channel": 9,
        "description": "G2B C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 9,
                "description": "G2B C2"
            },
            {
                "dvr_index": 1,
                "channel": 41,
                "description": "G2B C1"
            }
        ]
    },
    "GRADE 3B": {
        "dvr_index": 1,
        "channel": 10,
        "description": "G3B C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 10,
                "description": "G3B C2"
            },
            {
                "dvr_index": 1,
                "channel": 18,
                "description": "G3B C1"
            }
        ]
    },
    "Activity Room C1": {
        "dvr_index": 1,
        "channel": 11,
        "description": "Activity Room C1",
        "cam_type": ""
    },
    "GRADE 3C": {
        "dvr_index": 1,
        "channel": 13,
        "description": "G3C C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 13,
                "description": "G3C C2"
            },
            {
                "dvr_index": 1,
                "channel": 17,
                "description": "G3C C1"
            }
        ]
    },
    "GRADE 3A": {
        "dvr_index": 1,
        "channel": 14,
        "description": "G3A C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 14,
                "description": "G3A C1"
            },
            {
                "dvr_index": 1,
                "channel": 16,
                "description": "G3A C2"
            }
        ]
    },
    "Activity Room C2": {
        "dvr_index": 1,
        "channel": 19,
        "description": "Activity Room C2",
        "cam_type": ""
    },
    "GALLERY LIB 3": {
        "dvr_index": 1,
        "channel": 20,
        "description": "G Floor L/W Gall C1",
        "cam_type": ""
    },
    "PREP-3": {
        "dvr_index": 1,
        "channel": 21,
        "description": "PREP-3 C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 21,
                "description": "PREP-3 C1"
            },
            {
                "dvr_index": 1,
                "channel": 28,
                "description": "PREP-3 C2"
            }
        ]
    },
    "PREP-1": {
        "dvr_index": 1,
        "channel": 22,
        "description": "PREP 1 C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 22,
                "description": "PREP 1 C1"
            },
            {
                "dvr_index": 1,
                "channel": 33,
                "description": "PREP-1 C1"
            }
        ]
    },
    "BUS PARKING SIDE": {
        "dvr_index": 1,
        "channel": 23,
        "description": "BUS PARKING SIDE",
        "cam_type": ""
    },
    "GALLERY LIB 4": {
        "dvr_index": 1,
        "channel": 24,
        "description": "G Floor R/W Gall C2",
        "cam_type": ""
    },
    "PARK GENERATOR SIDE": {
        "dvr_index": 1,
        "channel": 25,
        "description": "PARK GENERATOR SIDE",
        "cam_type": ""
    },
    "PREP 1": {
        "dvr_index": 1,
        "channel": 26,
        "description": "PREP 1 C2",
        "cam_type": ""
    },
    "Popsicles": {
        "dvr_index": 1,
        "channel": 27,
        "description": "Popsicles C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 27,
                "description": "Popsicles C2"
            },
            {
                "dvr_index": 1,
                "channel": 30,
                "description": "Popsicles C1"
            }
        ]
    },
    "NUR-1": {
        "dvr_index": 1,
        "channel": 29,
        "description": "NUR-1 C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 29,
                "description": "NUR-1 C2"
            },
            {
                "dvr_index": 1,
                "channel": 37,
                "description": "NUR-1 C1"
            }
        ]
    },
    "ACTIVITY ROOM C2": {
        "dvr_index": 1,
        "channel": 31,
        "description": "ACTIVITY ROOM C2",
        "cam_type": ""
    },
    "GALLERY LIB 5": {
        "dvr_index": 1,
        "channel": 32,
        "description": "G Floor R/W Middel Str",
        "cam_type": ""
    },
    "ACTIVITY ROOM C1": {
        "dvr_index": 1,
        "channel": 34,
        "description": "ACTIVITY ROOM C1",
        "cam_type": ""
    },
    "NUR-2": {
        "dvr_index": 1,
        "channel": 35,
        "description": "NUR-2 C2",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 35,
                "description": "NUR-2 C2"
            },
            {
                "dvr_index": 1,
                "channel": 36,
                "description": "NUR-2 C1"
            }
        ]
    },
    "PREP-2": {
        "dvr_index": 1,
        "channel": 38,
        "description": "PREP-2 C1",
        "cam_type": "",
        "all_cameras": [
            {
                "dvr_index": 1,
                "channel": 38,
                "description": "PREP-2 C1"
            },
            {
                "dvr_index": 1,
                "channel": 39,
                "description": "PREP-2 C2"
            }
        ]
    },
    "NUR-3": {
        "dvr_index": 1,
        "channel": 40,
        "description": "NUR-3 C1",
        "cam_type": ""
    },
    "Dress Room Basement": {
        "dvr_index": 1,
        "channel": 42,
        "description": "Dress Room Basement",
        "cam_type": ""
    },
    "l 3 m": {
        "dvr_index": 1,
        "channel": 43,
        "description": "l 3 m",
        "cam_type": ""
    },
    "r 2 f": {
        "dvr_index": 1,
        "channel": 44,
        "description": "r 2 f",
        "cam_type": ""
    },
    "r3 m": {
        "dvr_index": 1,
        "channel": 45,
        "description": "r3 m",
        "cam_type": ""
    },
    "GALLERY LIB 6": {
        "dvr_index": 1,
        "channel": 46,
        "description": "G Floor L/W Gall C2",
        "cam_type": ""
    },
    "GALLERY LIB 7": {
        "dvr_index": 1,
        "channel": 47,
        "description": "RIGHT SIDE THIRD FLOOR CAM 1",
        "cam_type": ""
    },
    "GALLERY LIB 8": {
        "dvr_index": 1,
        "channel": 48,
        "description": "G Floor L/W Gall C2",
        "cam_type": ""
    },
    "R 1  3  F": {
        "dvr_index": 1,
        "channel": 49,
        "description": "R 1  3  F",
        "cam_type": ""
    },
    "Reception C1": {
        "dvr_index": 1,
        "channel": 54,
        "description": "Reception C1",
        "cam_type": ""
    },
    "Reception C2": {
        "dvr_index": 1,
        "channel": 55,
        "description": "Reception C2",
        "cam_type": ""
    },
    "Principal Room": {
        "dvr_index": 1,
        "channel": 56,
        "description": "Principal Room",
        "cam_type": ""
    },
    "Admission Room C1": {
        "dvr_index": 1,
        "channel": 57,
        "description": "Admission Room C1",
        "cam_type": ""
    },
    "Admin Room C1": {
        "dvr_index": 1,
        "channel": 58,
        "description": "Admin Room C1",
        "cam_type": ""
    },
    "Accounts Room": {
        "dvr_index": 1,
        "channel": 59,
        "description": "Accounts Room",
        "cam_type": ""
    },
    "MUSICE ROOM": {
        "dvr_index": 2,
        "channel": 1,
        "description": "MUSICE ROOM",
        "cam_type": ""
    },
    "GALLERY MID 1": {
        "dvr_index": 2,
        "channel": 2,
        "description": "SECOND FLOOR GALLERY",
        "cam_type": ""
    },
    "PARK GATE": {
        "dvr_index": 2,
        "channel": 3,
        "description": "PARK GATE",
        "cam_type": ""
    },
    "PARK BACK": {
        "dvr_index": 2,
        "channel": 5,
        "description": "PARK BACK CAMERA",
        "cam_type": ""
    },
    "GRADE 8A": {
        "dvr_index": 2,
        "channel": 9,
        "description": "G8A C1",
        "cam_type": ""
    },
    "GRADE 8B": {
        "dvr_index": 2,
        "channel": 10,
        "description": "G8B C1",
        "cam_type": ""
    },
    "ART ROOM": {
        "dvr_index": 2,
        "channel": 11,
        "description": "ART ROOM",
        "cam_type": ""
    },
    "GRADE 12A": {
        "dvr_index": 2,
        "channel": 12,
        "description": "G12A C1",
        "cam_type": ""
    },
    "GRADE 8C": {
        "dvr_index": 2,
        "channel": 13,
        "description": "G8C C1",
        "cam_type": ""
    },
    "PARK GENERATOR": {
        "dvr_index": 2,
        "channel": 14,
        "description": "PARK GENERATOR SIDE",
        "cam_type": ""
    },
    "PARK SWING": {
        "dvr_index": 2,
        "channel": 15,
        "description": "PARK SWING SIDE",
        "cam_type": ""
    },
    "ENTRY GATE- 2": {
        "dvr_index": 2,
        "channel": 16,
        "description": "ENTRY GATE- 2",
        "cam_type": ""
    },
    "GERMAN ROOM": {
        "dvr_index": 2,
        "channel": 18,
        "description": "GERMAN ROOM",
        "cam_type": ""
    }
}
