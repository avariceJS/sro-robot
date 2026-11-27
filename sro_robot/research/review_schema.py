"""Local review schema, separate from the unchanged 17-field evaluation schema."""
from scripts.dev.quality.court_dataset import FIELDS as EVALUATION_FIELDS

NAME_PARTS = ('last_name', 'first_name', 'middle_name')
FIELDS = EVALUATION_FIELDS + NAME_PARTS

# A separate explicit intake view; the debtor and evaluation schemas stay fixed.
APPOINTMENT_FIELDS = ('case_number', 'court', 'doc_date', 'judge_name', 'applicant',
                      'debtor_name', 'hearing_date', 'hearing_time', 'practitioner')
APPOINTMENT_REQUIRED = ('case_number', 'hearing_date', 'hearing_time')
PRACTITIONER_APPOINTMENT_REQUIRED = ('case_number', 'doc_date', 'practitioner')
