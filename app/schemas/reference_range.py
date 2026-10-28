from pydantic import BaseModel, ConfigDict, Field


class ReferenceRangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    parameter_key: str
    parameter_name: str
    reference_range: str
    unit: str
    source: str | None = None


class ReferenceRangeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    reference_range: str = Field(min_length=1, max_length=120)
    unit: str = Field(default="", max_length=40)


class ReferenceRangeSubmit(BaseModel):
    species: str = Field(min_length=1, max_length=60)
    # The lab that printed these ranges, when the report names one.
    source: str | None = Field(default=None, max_length=200)
    parameters: list[ReferenceRangeIn] = Field(max_length=200)


class ReferenceRangeMatch(BaseModel):
    species: str = Field(min_length=1, max_length=60)
    names: list[str] = Field(max_length=200)
