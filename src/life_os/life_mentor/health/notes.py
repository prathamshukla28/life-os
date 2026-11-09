"""Short, cited, evidence-class-tagged explanation notes (LM06).

Each note is a ``life_os.core.evidence.claim`` whose ``basis`` is the source URL
it paraphrases. Notes only restate what the cited page says; no mechanism beyond
the source is added (no invented physiology).
"""

from __future__ import annotations

from life_os.core.evidence import STRONG_EVIDENCE, VERIFIED_FACT, claim

__all__ = ["FAO_URL", "ICMR_URL", "MECHANISM_NOTES", "NOTE_BY_ID"]

ICMR_URL = "https://www.nin.res.in/rdabook/brief_note.pdf"
FAO_URL = "https://www.fao.org/4/i3124e/i3124e.pdf"
_MED = "https://medlineplus.gov/druginfo/meds/"

NOTE_BY_ID: dict[str, dict[str, str]] = {
    "protein_reference": claim(
        "ICMR-NIN 2020 gives an EAR of 0.66 and an RDA of 0.83 g protein/kg/day for healthy "
        "Indian adults, and 1 g/kg/day for cereal-based diets with low-quality protein.",
        VERIFIED_FACT,
        ICMR_URL,
    ),
    "diaas_definition": claim(
        "FAO (2013): DIAAS compares each digestible indispensable amino acid in 1 g of dietary "
        "protein with the same amino acid in 1 g of a reference protein; the lowest ratio counts.",
        VERIFIED_FACT,
        FAO_URL,
    ),
    "warfarin_vitamin_k": claim(
        "MedlinePlus: foods containing vitamin K can affect how warfarin works; it advises "
        "consistent vitamin K intake week to week.",
        STRONG_EVIDENCE,
        _MED + "a682277.html",
    ),
    "maoi_tyramine": claim(
        "MedlinePlus: foods high in tyramine can cause a serious reaction during phenelzine treatment.",
        STRONG_EVIDENCE,
        _MED + "a682089.html",
    ),
    "grapefruit_statins": claim(
        "MedlinePlus: talk to your doctor about grapefruit or grapefruit juice while taking "
        "simvastatin or atorvastatin.",
        STRONG_EVIDENCE,
        _MED + "a692030.html",
    ),
    "ace_inhibitor_potassium": claim(
        "MedlinePlus: talk to your doctor before using potassium-containing salt substitutes "
        "while taking lisinopril.",
        STRONG_EVIDENCE,
        _MED + "a692051.html",
    ),
    "kidney_disease_potassium": claim(
        "NIDDK: with chronic kidney disease, limiting foods high in sodium, potassium and "
        "phosphorus may prevent or delay some health problems.",
        STRONG_EVIDENCE,
        "https://www.niddk.nih.gov/health-information/kidney-disease/chronic-kidney-disease-ckd/"
        "eating-nutrition",
    ),
}

MECHANISM_NOTES: tuple[dict[str, str], ...] = tuple(NOTE_BY_ID.values())
