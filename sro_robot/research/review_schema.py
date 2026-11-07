"""Local review schema, separate from the unchanged 17-field evaluation schema."""
from scripts.dev.quality.court_dataset import FIELDS as EVALUATION_FIELDS

NAME_PARTS = ('last_name', 'first_name', 'middle_name')
FIELDS = EVALUATION_FIELDS + NAME_PARTS
