"""JSON contracts for language identification, translation and semantic review."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LanguageCode = Literal["en", "ar", "ur", "ml", "tl", "und"]
BRIDGE_LANGUAGES = frozenset({"ur", "ml", "tl"})
SUPPORTED_LANGUAGES = frozenset({"en", "ar", "ur", "ml", "tl"})


class LanguageDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    detected_language: LanguageCode
    target_language: LanguageCode
    needs_clarification: bool = False


class TranslationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def validate_text(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("Translation text must not be blank")
        return text


class SemanticDifference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_quote: str = Field(max_length=200)
    candidate_quote: str = Field(max_length=200)
    explanation: str = Field(min_length=1, max_length=300)


class SemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_meaning: str = Field(min_length=1, max_length=800)
    candidate_meaning: str = Field(min_length=1, max_length=800)
    verdict: Literal["equivalent", "different", "ambiguous"]
    differences: list[SemanticDifference] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_evidence(self) -> SemanticReview:
        if (self.verdict == "equivalent") != (not self.differences):
            raise ValueError("Review verdict and evidence disagree")
        if any(
            not item.source_quote and not item.candidate_quote
            for item in self.differences
        ):
            raise ValueError("Review differences require a source or candidate quote")
        return self


class LanguageBridgeError(ValueError):
    """A conversion cannot safely proceed; business execution must stop."""
