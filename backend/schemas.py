from typing import Optional
from pydantic import BaseModel, Field


class StudentReservationCreate(BaseModel):
    room_id: str
    reservation_date: str  # YYYY-MM-DD
    start_time: str        # HH:MM
    end_time: str          # HH:MM
    purpose: str = "Individual Study"
    expected_attendees: int = Field(default=1, ge=1, le=50)


class SessionJoinRequest(BaseModel):
    group_size: int = Field(default=1, ge=1, le=20)
    notes: str = ""


class ClubEventCreate(BaseModel):
    club_id: int
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=5, max_length=600)
    category: str = "Workshop"
    event_date: str        # YYYY-MM-DD
    start_time: str        # HH:MM
    end_time: str          # HH:MM
    room_id: str
    expected_participants: int = Field(ge=2, le=200)
    is_open_to_all: bool = True


class ClubEventUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    expected_participants: Optional[int] = Field(default=None, ge=2, le=200)
    is_open_to_all: Optional[bool] = None


class AdminRoomCreate(BaseModel):
    id: str
    name: str
    building: str = "Beshyogoch Main Campus"
    floor: int
    room_type: str = "Seminar Room"
    capacity: int = Field(ge=2, le=500)
    amenities: str = ""
    access_mode: str = "shared"  # student_only | club_only | shared | unavailable
    open_time: str = "08:00"
    close_time: str = "21:00"
    buffer_minutes: int = 0


class AdminRoomPermissionUpdate(BaseModel):
    access_mode: str  # student_only | club_only | shared | unavailable
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    buffer_minutes: Optional[int] = None
    notes: Optional[str] = None


class AdminScheduleCreate(BaseModel):
    room_id: str
    day_of_week: str
    start_time: str
    end_time: str
    course_title: str
    instructor: str = ""


class AdminRoomBlockCreate(BaseModel):
    room_id: str
    exception_date: str
    start_time: str
    end_time: str
    reason: str
    exception_type: str = "admin_block"  # maintenance | admin_block | special_event | closure


class AdminClubLeaderApprove(BaseModel):
    user_id: int
    club_id: int
    action: str = "approve"  # approve | revoke


class AdminEventReview(BaseModel):
    decision: str  # approve | reject
    note: str = ""


class AdminSessionCreate(BaseModel):
    session_type: str  # quiet | collaboration
    title: str
    description: str
    room_id: str
    session_date: str
    start_time: str
    end_time: str
    max_capacity: int = Field(ge=3, le=100)
    min_group_size: int = Field(default=1, ge=1, le=10)
    max_groups: Optional[int] = None
