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
    },
    {
        "name": "DVR 4",
        "ip": "192.168.0.13",
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
    },
    "Reception C4": {
        "dvr_index": 1,
        "channel": 52,
        "description": "Reception C4",
        "cam_type": ""
    },
    "Reception C3": {
        "dvr_index": 1,
        "channel": 53,
        "description": "Reception C3",
        "cam_type": ""
    },
    "ENTRY GATE-1": {
        "dvr_index": 2,
        "channel": 20,
        "description": "ENTRY GATE-1",
        "cam_type": ""
    },
    "ASSEMBLY AREA": {
        "dvr_index": 2,
        "channel": 21,
        "description": "ASSEMBLY AREA",
        "cam_type": ""
    },
    "SPORTS ROOM": {
        "dvr_index": 2,
        "channel": 22,
        "description": "SPORTS ROOM",
        "cam_type": ""
    },
    "Administration": {
        "dvr_index": 2,
        "channel": 23,
        "description": "Administration",
        "cam_type": ""
    },
    "Front Gallery": {
        "dvr_index": 3,
        "channel": 1,
        "description": "Front Gallery",
        "cam_type": ""
    },
    "Basement R/W Middle Strs": {
        "dvr_index": 3,
        "channel": 6,
        "description": "Basement R/W Middle Strs",
        "cam_type": ""
    },
    "G Floor R/W Gall C1": {
        "dvr_index": 3,
        "channel": 9,
        "description": "G Floor R/W Gall C1",
        "cam_type": ""
    },
    "Basement Cam 8": {
        "dvr_index": 3,
        "channel": 10,
        "description": "Basement Cam 8",
        "cam_type": ""
    },
    "Basement Electricity": {
        "dvr_index": 3,
        "channel": 11,
        "description": "Basement Electricity",
        "cam_type": ""
    },
    "Basement Main Gate": {
        "dvr_index": 3,
        "channel": 12,
        "description": "Basement Main Gate",
        "cam_type": ""
    },
    "G Floor Recpt Back Gall C1": {
        "dvr_index": 3,
        "channel": 13,
        "description": "G Floor Recpt Back Gall C1",
        "cam_type": ""
    },
    "TRANSPORT ROOM": {
        "dvr_index": 3,
        "channel": 15,
        "description": "TRANSPORT ROOM",
        "cam_type": ""
    },
    "IT Room": {
        "dvr_index": 3,
        "channel": 16,
        "description": "IT Room",
        "cam_type": ""
    },
    "Kitchen Room": {
        "dvr_index": 3,
        "channel": 18,
        "description": "Kitchen Room",
        "cam_type": ""
    },
    "Basement Cam 10": {
        "dvr_index": 3,
        "channel": 19,
        "description": "Basement Cam 10",
        "cam_type": ""
    },
    "Basement L/W Middle Strs": {
        "dvr_index": 3,
        "channel": 20,
        "description": "Basement L/W Middle Strs",
        "cam_type": ""
    },
    "Basement Cam 2": {
        "dvr_index": 3,
        "channel": 21,
        "description": "Basement Cam 2",
        "cam_type": ""
    },
    "Orchestra Basement": {
        "dvr_index": 3,
        "channel": 23,
        "description": "Orchestra Basement",
        "cam_type": ""
    },
    "Basement Generator Right Exit": {
        "dvr_index": 3,
        "channel": 25,
        "description": "Basement Generator Right Exit",
        "cam_type": ""
    },
    "Admin Gallery": {
        "dvr_index": 3,
        "channel": 26,
        "description": "Admin Gallery",
        "cam_type": ""
    },
    "Medical Room": {
        "dvr_index": 3,
        "channel": 27,
        "description": "Medical Room",
        "cam_type": ""
    },
    "IMN A/C 2": {
        "dvr_index": 3,
        "channel": 29,
        "description": "IMN A/C 2",
        "cam_type": ""
    },
    "G Floor R/W Bus Parking": {
        "dvr_index": 3,
        "channel": 30,
        "description": "G Floor R/W Bus Parking",
        "cam_type": ""
    },
    "OUTDOOR CAM 1": {
        "dvr_index": 3,
        "channel": 33,
        "description": "OUTDOOR CAM 1",
        "cam_type": ""
    },
    "DVR4 SPORTS ROOM": {
        "dvr_index": 3,
        "channel": 34,
        "description": "SPORTS ROOM",
        "cam_type": ""
    },
    "YOGA ROOM": {
        "dvr_index": 3,
        "channel": 36,
        "description": "YOGA ROOM",
        "cam_type": ""
    },
    "Basement Cam 5": {
        "dvr_index": 3,
        "channel": 37,
        "description": "Basement Cam 5",
        "cam_type": ""
    },
    "DVR4 ASSEMBLY AREA": {
        "dvr_index": 3,
        "channel": 40,
        "description": "ASSEMBLY AREA",
        "cam_type": ""
    },
    "IMN A/C 1": {
        "dvr_index": 3,
        "channel": 41,
        "description": "IMN A/C 1",
        "cam_type": ""
    },
    "Basement R/W First Strs": {
        "dvr_index": 3,
        "channel": 42,
        "description": "Basement R/W First Strs",
        "cam_type": ""
    },
}

SEED_CAMERA_MAPPING.update({
    "NUR-3": {
        "dvr_index": 1, "channel": 22, "description": "NUR-3 C1", "cam_type": "C1",
        "all_cameras": [
            {"dvr_index": 1, "channel": 22, "description": "NUR-3 C1", "cam_type": "C1"},
            {"dvr_index": 1, "channel": 26, "description": "NUR-3 C2", "cam_type": "C2"},
        ],
    },
    "PREP-1": {
        "dvr_index": 1, "channel": 33, "description": "PREP-1 C1", "cam_type": "C1",
        "all_cameras": [
            {"dvr_index": 1, "channel": 33, "description": "PREP-1 C1", "cam_type": "C1"},
            {"dvr_index": 1, "channel": 40, "description": "PREP-1 C2", "cam_type": "C2"},
        ],
    },
    "DANCE ROOM BASEMENT": {
        "dvr_index": 2, "channel": 18, "description": "DANCE ROOM BASEMENT", "cam_type": "",
    },
    "ASSEMBLY AREA": {
        "dvr_index": 2, "channel": 21, "description": "ASSEMBLY AREA", "cam_type": "",
        "all_cameras": [
            {"dvr_index": 2, "channel": 21, "description": "ASSEMBLY AREA", "cam_type": ""},
            {"dvr_index": 3, "channel": 40, "description": "ASSEMBLY AREA", "cam_type": ""},
        ],
    },
    "SPORTS ROOM": {
        "dvr_index": 2, "channel": 22, "description": "SPORTS ROOM", "cam_type": "",
        "all_cameras": [
            {"dvr_index": 2, "channel": 22, "description": "SPORTS ROOM", "cam_type": ""},
            {"dvr_index": 3, "channel": 34, "description": "SPORTS ROOM", "cam_type": ""},
        ],
    },
    "3rd Floor Gallery 1": {
        "dvr_index": 2, "channel": 17, "description": "3rd Floor Gallery 1", "cam_type": "",
    },
    "3rd Floor Gallery 2": {
        "dvr_index": 2, "channel": 21, "description": "3rd Floor Gallery 2", "cam_type": "",
    },
    "G11D C1": {
        "dvr_index": 2, "channel": 26, "description": "G11D C1", "cam_type": "C1",
    },
    "GRADE 11D": {
        "dvr_index": 2, "channel": 26, "description": "G11D C1", "cam_type": "C1",
    },
    "3rd Floor T.Staff": {
        "dvr_index": 2, "channel": 28, "description": "3rd Floor T.Staff", "cam_type": "",
    },
    "YOGA  ROOM": {
        "dvr_index": 3, "channel": 36, "description": "YOGA  ROOM", "cam_type": "",
    },
})
for _obsolete_location in (
    "PREP 1", "DVR4 SPORTS ROOM", "YOGA ROOM", "DVR4 ASSEMBLY AREA",
):
    SEED_CAMERA_MAPPING.pop(_obsolete_location, None)

# ---------------------------------------------------------------------------
# Homework Google Doc mapping — one doc per class/section
# Created in BOT-HOMEWORK@ppischool.in Drive folder "PPIS CW_HW_ 2026-27"
# ---------------------------------------------------------------------------
SEED_HOMEWORK_DOCS = {
    "Nursery 1": {"doc_id": "1SapBzenY8dAFuiOsF8lj7IqGy0dzJCpNCmquLpIBc4s", "url": "https://docs.google.com/document/d/1SapBzenY8dAFuiOsF8lj7IqGy0dzJCpNCmquLpIBc4s/edit"},
    "Nursery 2": {"doc_id": "168NN7DsWpDTNOHhqdxMuMGdGpTZ6WSRuzuWHZvWk6oc", "url": "https://docs.google.com/document/d/168NN7DsWpDTNOHhqdxMuMGdGpTZ6WSRuzuWHZvWk6oc/edit"},
    "Nursery 3": {"doc_id": "15WtnUVVyIFkT2IhW0uCt2eRoSZUBCrQLYQGX_34HTEI", "url": "https://docs.google.com/document/d/15WtnUVVyIFkT2IhW0uCt2eRoSZUBCrQLYQGX_34HTEI/edit"},
    "Prep 1": {"doc_id": "17q38v-pE3svoplheNkUi7a8uv5Wip5Ikzjr5Nl_dajk", "url": "https://docs.google.com/document/d/17q38v-pE3svoplheNkUi7a8uv5Wip5Ikzjr5Nl_dajk/edit"},
    "Prep 2": {"doc_id": "1xoqWy3rj6EuxI9iZnkY8aFzy-Iw6DLb219KJHyTtRqI", "url": "https://docs.google.com/document/d/1xoqWy3rj6EuxI9iZnkY8aFzy-Iw6DLb219KJHyTtRqI/edit"},
    "Prep 3": {"doc_id": "1UViVxFGua1iEosmKLq27Q9CR3FbavM_hPDQZdDPK5lw", "url": "https://docs.google.com/document/d/1UViVxFGua1iEosmKLq27Q9CR3FbavM_hPDQZdDPK5lw/edit"},
    "Popsicles": {"doc_id": "1AT9D2MVHMGbj0QSHp0VPPjAJigu4PsC-O_jtePxyLPQ", "url": "https://docs.google.com/document/d/1AT9D2MVHMGbj0QSHp0VPPjAJigu4PsC-O_jtePxyLPQ/edit"},
    "Grade 1A": {"doc_id": "1v_VriuJ8FcfrxZXUCFsWBeuoxf1o0MwPq10JqOgLctE", "url": "https://docs.google.com/document/d/1v_VriuJ8FcfrxZXUCFsWBeuoxf1o0MwPq10JqOgLctE/edit"},
    "Grade 1B": {"doc_id": "1fdmctA43YP3v8b-xrbAtsCqRmTgr8ICiopDOJnQWpK8", "url": "https://docs.google.com/document/d/1fdmctA43YP3v8b-xrbAtsCqRmTgr8ICiopDOJnQWpK8/edit"},
    "Grade 2A": {"doc_id": "1oOIB4Q6H1fzuf3lrcmT8Cu7ZbG8v4SzsHLPjDO8zV9c", "url": "https://docs.google.com/document/d/1oOIB4Q6H1fzuf3lrcmT8Cu7ZbG8v4SzsHLPjDO8zV9c/edit"},
    "Grade 2B": {"doc_id": "1mvZXtLuvT495Y8XNeZLUevHRMxHCjI-swFcft4FI1bA", "url": "https://docs.google.com/document/d/1mvZXtLuvT495Y8XNeZLUevHRMxHCjI-swFcft4FI1bA/edit"},
    "Grade 3A": {"doc_id": "10dcbzfKI4PVxeGkXFDkQ8XErlCeTYTdFLKf-xF3XsAI", "url": "https://docs.google.com/document/d/10dcbzfKI4PVxeGkXFDkQ8XErlCeTYTdFLKf-xF3XsAI/edit"},
    "Grade 3B": {"doc_id": "1St3fxGSfSu7zZbt1BIPKREXOELBtoPxpKI9jicRWdQI", "url": "https://docs.google.com/document/d/1St3fxGSfSu7zZbt1BIPKREXOELBtoPxpKI9jicRWdQI/edit"},
    "Grade 3C": {"doc_id": "1ptkk8UqNNncAq0LGbrbPhhWXugCO0KL0wlPAbP7OO7w", "url": "https://docs.google.com/document/d/1ptkk8UqNNncAq0LGbrbPhhWXugCO0KL0wlPAbP7OO7w/edit"},
    "Grade 4A": {"doc_id": "12ZIg20mqT8Hb29bVItG0wN27wFt9-u5IXhy2tfBkCOc", "url": "https://docs.google.com/document/d/12ZIg20mqT8Hb29bVItG0wN27wFt9-u5IXhy2tfBkCOc/edit"},
    "Grade 4B": {"doc_id": "1B2Zlu16LzXqD96iuNdLm4M6cKu0X_r_thn2k5aASVPo", "url": "https://docs.google.com/document/d/1B2Zlu16LzXqD96iuNdLm4M6cKu0X_r_thn2k5aASVPo/edit"},
    "Grade 5A": {"doc_id": "1pzKM5-ZrzZUeSPOsobZql47VFcpj3i6sf6azRQ-0cjg", "url": "https://docs.google.com/document/d/1pzKM5-ZrzZUeSPOsobZql47VFcpj3i6sf6azRQ-0cjg/edit"},
    "Grade 5B": {"doc_id": "1ejoIXGOwGuPdbEnxQaMFvVXXviqPAL1IvVLqx5B0bfo", "url": "https://docs.google.com/document/d/1ejoIXGOwGuPdbEnxQaMFvVXXviqPAL1IvVLqx5B0bfo/edit"},
    "Grade 6A": {"doc_id": "1ig8vHSSOfBXaLGTm5Crl4h6D-GrwnIvRO1PvSwjBp2E", "url": "https://docs.google.com/document/d/1ig8vHSSOfBXaLGTm5Crl4h6D-GrwnIvRO1PvSwjBp2E/edit"},
    "Grade 6B": {"doc_id": "1D9UdbcP89ZOA7F4YdKkBizM3Lj6ATXLW-4nPJ6FhR8A", "url": "https://docs.google.com/document/d/1D9UdbcP89ZOA7F4YdKkBizM3Lj6ATXLW-4nPJ6FhR8A/edit"},
    "Grade 7A": {"doc_id": "1xffzcg3pbS_wPm6vdSF0IPgxIoAl8He0oS0cL0sA4f8", "url": "https://docs.google.com/document/d/1xffzcg3pbS_wPm6vdSF0IPgxIoAl8He0oS0cL0sA4f8/edit"},
    "Grade 7B": {"doc_id": "1qSF2-h2vn_CLwc4pTy0LQHkLPOsXyph7p5y_SwwO5xQ", "url": "https://docs.google.com/document/d/1qSF2-h2vn_CLwc4pTy0LQHkLPOsXyph7p5y_SwwO5xQ/edit"},
    "Grade 8A": {"doc_id": "1HAUK6dgxhQJG342JvzxkW6srzXSTB5m35-lx1ID_H_I", "url": "https://docs.google.com/document/d/1HAUK6dgxhQJG342JvzxkW6srzXSTB5m35-lx1ID_H_I/edit"},
    "Grade 8B": {"doc_id": "1qMZDYmiSSdhNH5J6GkZY0SM_Cgi4FIrnjoD0no8ayiM", "url": "https://docs.google.com/document/d/1qMZDYmiSSdhNH5J6GkZY0SM_Cgi4FIrnjoD0no8ayiM/edit"},
    "Grade 8C": {"doc_id": "1VNlVzIZ-SMMQpEIFsXBj2SfcsUN11UNMT7zEiQDJh0w", "url": "https://docs.google.com/document/d/1VNlVzIZ-SMMQpEIFsXBj2SfcsUN11UNMT7zEiQDJh0w/edit"},
    "Grade 9A": {"doc_id": "1KfEJv9kINll_yQMHPLtUIz4O7qiyrYUsPhPx1n9ubIg", "url": "https://docs.google.com/document/d/1KfEJv9kINll_yQMHPLtUIz4O7qiyrYUsPhPx1n9ubIg/edit"},
    "Grade 9B": {"doc_id": "1MC16RcYGVhcS8v6ZbqHaKUP8u68kSbGoVQx9YQlImLA", "url": "https://docs.google.com/document/d/1MC16RcYGVhcS8v6ZbqHaKUP8u68kSbGoVQx9YQlImLA/edit"},
    "Grade 9C": {"doc_id": "1edJE_KodRNSODJT1Bhbpl3M_YjTKdqPNgpxwnExOJk8", "url": "https://docs.google.com/document/d/1edJE_KodRNSODJT1Bhbpl3M_YjTKdqPNgpxwnExOJk8/edit"},
    "Grade 10A": {"doc_id": "1UbGVV-9l9LKqx71GxKpvtfJVf6ncdmVjPOzb2SbwJPg", "url": "https://docs.google.com/document/d/1UbGVV-9l9LKqx71GxKpvtfJVf6ncdmVjPOzb2SbwJPg/edit"},
    "Grade 10B": {"doc_id": "1Vp7LgkUGnMUO-rRxLhe4eEo-USZvpxGesv_fKpBp4Os", "url": "https://docs.google.com/document/d/1Vp7LgkUGnMUO-rRxLhe4eEo-USZvpxGesv_fKpBp4Os/edit"},
    "Grade 11A-SCIENCE": {"doc_id": "1KuiVvOS8v9o8Bu4vrXSLuIrIkUZX56B-FdmsmMkVJuo", "url": "https://docs.google.com/document/d/1KuiVvOS8v9o8Bu4vrXSLuIrIkUZX56B-FdmsmMkVJuo/edit"},
    "Grade 11B-COMMERCE": {"doc_id": "1i2epU_zJk0dbCT4x7vFFpGdv3LF1xFD6SosxEpjqYwc", "url": "https://docs.google.com/document/d/1i2epU_zJk0dbCT4x7vFFpGdv3LF1xFD6SosxEpjqYwc/edit"},
    "Grade 11C-HUMANITIES": {"doc_id": "", "url": ""},  # Will be created and filled via API
    "Grade 12A-SCIENCE": {"doc_id": "1FwUe8EIfrhfDpKugImOzHLlCe0tCguTxPF29vRWBZhs", "url": "https://docs.google.com/document/d/1FwUe8EIfrhfDpKugImOzHLlCe0tCguTxPF29vRWBZhs/edit"},
    "Grade 12B-COMMERCE": {"doc_id": "1_vu9gKBxQnoUtkZOmF7UMSLwWGL4ltaryM-Z79JnPro", "url": "https://docs.google.com/document/d/1_vu9gKBxQnoUtkZOmF7UMSLwWGL4ltaryM-Z79JnPro/edit"},
    "Grade 12C-HUMANITIES": {"doc_id": "", "url": ""},  # Will be created and filled via API
}
