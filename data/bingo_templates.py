"""Static data for the Human Bingo game.

Two kinds of content live here, both entered once by hand at build time
(never computed at runtime):

* SHEETS — the 25 printed prompt strings for each of the 15 card templates,
  transcribed by reading data/bingo_templates/1.png .. 15.png. The centre of
  every card is the FREE SPACE wildcard.
* grid geometry — six fractional constants measured once off 1.png. Every
  card shares the same 5x5 layout, so all 25 crop boxes derive from those
  numbers via _build_cell_boxes() rather than 375 hand-typed boxes.
  Boxes are fractions of the image width/height, so they land correctly after
  the OCR step normalises an upload to any resolution.
"""

import pathlib

# --- Layout -----------------------------------------------------------------
GRID = 5                 # 5x5 card
NUM_SHEETS = 15          # templates 1.png .. 15.png
FREE = (2, 2)            # (row, col) of the free centre wildcard

TEMPLATE_DIR = pathlib.Path(__file__).parent / "bingo_templates"


# --- Measured grid geometry (fractions of image width/height) ---------------
# MEASURED once off 1.png using OpenCV contour detection on adaptive threshold.
# Image size: W=1348 H=2000. All 15 templates share this geometry.
# These MUST be the real measured values (the crop-accuracy test enforces it).
ORIGIN_X_F = 0.1061      # left edge of cell (0,0)'s box
ORIGIN_Y_F = 0.2260      # top edge of cell (0,0)'s box
CELL_W_F   = 0.1439      # one cell's width
CELL_H_F   = 0.1060      # one cell's height
GUTTER_F   = 0.0152      # gap between adjacent cell boxes

# Fractional box around the printed sheet number, top-left margin. MEASURED.
# Sheet number "1" appears at approx px (30,30)-(100,110) in W=1348 H=2000.
CORNER_BOX = (0.0223, 0.0150, 0.0742, 0.0550)


def _build_cell_boxes():
    """Derive all 25 fractional (x0,y0,x1,y1) crop boxes from the measured
    grid geometry. Shared by every sheet; the free centre is included so
    callers have a complete grid, but OCR skips it via is_free()."""
    boxes = {}
    for r in range(GRID):
        for c in range(GRID):
            x0 = ORIGIN_X_F + c * (CELL_W_F + GUTTER_F)
            y0 = ORIGIN_Y_F + r * (CELL_H_F + GUTTER_F)
            x1 = x0 + CELL_W_F
            y1 = y0 + CELL_H_F
            boxes[(r, c)] = (x0, y0, x1, y1)
    return boxes


CELL_BOXES = _build_cell_boxes()


# --- Transcribed prompts ----------------------------------------------------
# SHEETS[n] is a 5x5 grid of the printed prompts on template n.png, in reading
# order (row 0 = top, col 0 = left). SHEETS[n][2][2] is always "FREE SPACE".
# Transcribed by reading each PNG image directly.
SHEETS = {
    1: [
        ["Studying the same major as you", "Is an international student", "Plays a musical instrument", "Took a gap year before university", "Birthday is in February"],
        ["Can cook", "Loves pineapple on pizza", "Is left-handed", "Is afraid of heights", "Has never eaten fast food"],
        ["Has a pet", "Is an only child", "FREE SPACE", "Is an athlete", "Favourite colour is green"],
        ["Has worked part time", "Loves coffee", "Has nephews or nieces", "Loves reading", "Served National Service"],
        ["Likes spicy food", "Has a partner", "Knows French", "Plays Minecraft", "Loves movies"],
    ],
    2: [
        ["Studying Law", "Birthday is in November", "Is not single", "Plays Minecraft", "Hates reading"],
        ["Can cook", "Loves pineapple on pizza", "Plays squash", "Is afraid of heights", "Is the youngest sibling"],
        ["Loves coffee", "Hates veggies", "FREE SPACE", "Likes spicy food", "Is the middle child"],
        ["Has nephews", "Loves movies", "Hasnt worked part time before", "Is an international student", "Knows French"],
        ["Is left-handed", "Plays the violin", "Favourite colour is red", "Took a gap year before university", "Has a bird as a pet"],
    ],
    3: [
        ["Afraid of spiders", "Is a local", "Plays a musical instrument", "Took a gap year before university", "Has worked part time"],
        ["Loves tea", "Loves pineapple on pizza", "Is left-handed", "Is in the School of Computing", "Has never eaten fast food"],
        ["Has a pet", "Interested in Philosophy", "FREE SPACE", "Good at drawing", "Favourite colour is purple"],
        ["Birthday is in June", "Can cook", "Has nephews or nieces", "Plays badminton", "Speaks more than 2 languages"],
        ["Is single", "Is the same age as you", "Cant handle spice", "Has siblings", "Loves movies"],
    ],
    4: [
        ["Has nephew or nieces", "Took a gap year before university", "Plays a musical instrument", "Is in the School of Medicine", "Has worked part time"],
        ["Loves tea", "Is a December baby", "Is left-handed", "Afraid of heights", "Is a Singaporean"],
        ["Favourite colour is purple", "Interested in Philosophy", "FREE SPACE", "Good at drawing", "Loves movies"],
        ["Hates pickles", "Can cook", "Has never eaten fast food", "Cant handle spice", "Speaks more than 2 languages"],
        ["Is single", "Is the same age as you", "Plays Ultimate Frisbee", "Has siblings", "Has a pet"],
    ],
    5: [
        ["Knows a martial art", "Has never been to Singapore", "Broken a bone before", "Loves movies", "Birthday is in the same month as you"],
        ["Can cook", "Cant ride a bike", "Is left-handed", "Plays a musical instrument", "Hates pineapple on pizza"],
        ["Is in the Faculty of Science", "Is an only child", "FREE SPACE", "Favourite colour is red", "Is the same age as you"],
        ["Plays Roblox", "Loves matcha", "Is afraid of heights", "Plays badminton", "Speaks more than 2 languages"],
        ["Has a pet", "Is single", "Cant handle spice", "Has never eaten fast food", "Has a film camera"],
    ],
    6: [
        ["Knows a martial art", "Has never been to Singapore", "Broken a bone before", "Loves movies", "Birthday is in the same month as you"],
        ["Can cook", "Cant ride a bike", "Is left-handed", "Plays a musical instrument", "Hates pineapple on pizza"],
        ["Is a Business major", "Is an only child", "FREE SPACE", "Favourite colour is red", "Is the same age as you"],
        ["Plays Roblox", "Loves matcha", "Is afraid of heights", "Plays badminton", "Speaks more than 2 languages"],
        ["Has a pet", "Is single", "Cant handle spice", "Has never eaten fast food", "Has a film camera"],
    ],
    7: [
        ["Has 3 or more siblings", "Has never been to Singapore", "Has eaten a bizarre local delicacy", "Has a sibling who is already married", "Likes cozy video games"],
        ["Can cook", "Cant ride a bike", "Is left-handed", "Plays a musical instrument", "Hates pineapple on pizza"],
        ["Likes a specific fast food chain", "Is an only child", "FREE SPACE", "Speaks more than 2 languages", "Is the same age as you"],
        ["Plays chess", "Loves matcha", "Is afraid of heights", "Plays badminton", "Favourite colour is yellow"],
        ["Has a pet", "Has a tattoo", "Cant handle spice", "Is in the Faculty of Science", "Is a night owl"],
    ],
    8: [
        ["Favourite colour is yellow", "Has never been to Singapore", "Has eaten a bizarre local delicacy", "Is older than you", "Likes cozy video games"],
        ["Hates fast food", "Cant ride a bike", "Speaks more than 2 languages", "Plays a musical instrument", "Loves matcha"],
        ["Has a pet", "Is an only child", "FREE SPACE", "Can draw", "Has a sibling who is already married"],
        ["Plays chess", "Loves pineapple on pizza", "Is afraid of heights", "Plays badminton", "Has 3 or more siblings"],
        ["Cant function past 10pm", "Has a tattoo", "Cant handle spice", "Is in the Faculty of Science", "Is left-handed"],
    ],
    9: [
        ["Knows a martial art", "Takes notes exclusively on a tablet", "Broken a bone before", "Loves movies", "Birthday is in the same month as you"],
        ["Prefers studying past midnight", "Has never eaten fast food", "Has never been to NUS", "Went to a concert in the past year", "Hates pineapple on pizza"],
        ["Is in the Faculty of Science", "Is an only child", "FREE SPACE", "Favourite colour is red", "Is the same age as you"],
        ["Can solve a Rubik's cube", "Has a film camera", "Is afraid of heights", "Plays badminton", "Speaks more than 2 languages"],
        ["Has a pet", "Has met a celebrity in person", "Has a sweet tooth", "Cant ride a bike", "Loves matcha"],
    ],
    10: [
        ["Prefers physical books over e-books", "Has a sibling who is already married", "Is ambidextrous", "Has run a half-marathon or full marathon", "Has more than two pets at home"],
        ["Prefers coffee over tea", "Was born in a different country than you", "Was born on a leap year", "Loves pineapple on pizza", "Can bake anything completely from scratch"],
        ["Is in the Faculty of Science", "Doesnt play video games", "FREE SPACE", "Favourite colour is red", "Is the same age as you"],
        ["Transferred from another college", "Loves matcha", "Is afraid of heights", "Plays badminton", "Speaks more than 2 languages"],
        ["Has a pet", "Can sing", "Cant handle spice", "Is currently trying to learn a brand-new instrument", "Hates riding rollercoasters"],
    ],
    11: [
        ["Speaks at least 2 languages", "Can cook", "Loves shooter games", "Is an only child", "Is a 18 hour flight away from Singapore"],
        ["Prefers tea over coffee", "Same major as you", "Shares your exact birth month", "Plays an instrument", "Has worked specifically in retail or food service"],
        ["Dips their fries in ice cream", "Is terrified of insects", "FREE SPACE", "Has a pet other than a dog or cat", "Is ambidextrous"],
        ["Is currently learning on Duolingo", "Represents a sports team", "Is afraid of heights", "Has a sibling who is already married", "Speaks more than 2 languages"],
        ["Is the same age as you", "Loves extremely spicy food", "Hates reading", "Has never eaten fast food", "Goes to the cinema at least once a month"],
    ],
    12: [
        ["Goes to the gym", "Shares your exact birth month", "Broken a bone before", "Needs at least two cups of coffee", "Needs spice to survive"],
        ["Is officially an aunt/uncle", "Cant ride a bike", "Can cook", "Plays the drums", "Has a film camera"],
        ["Read more than 10 books last year", "Is single", "FREE SPACE", "Is in the same faculty as you", "Is the same age as you"],
        ["Has logged over 500 hours in a video game", "Loves matcha", "Is afraid of spiders", "Was born in the same country as you", "Speaks more than 2 languages"],
        ["Is an only child", "Has brown hair", "Loves running", "Has never eaten fast food", "Has worked specifically in F&B or retail"],
    ],
    13: [
        ["Has a side hustle", "Is wearing something yellow right now", "Broken a bone before", "Is a strict vegetarian or vegan", "Cooks for their family"],
        ["Plays competitive video games", "Is a huge fan of fantasy or sci-fi novels", "Knows sign language", "Has a pet", "Prefers savory over sweet"],
        ["Loves spice", "Has the same minor or second major as you", "FREE SPACE", "Goes to the gym", "Has a partner"],
        ["Only drinks iced coffee", "Regularly babysits their younger relatives", "Worked full-time before starting university", "Plays badminton", "Speaks more than 2 languages"],
        ["Refuses to watch horror movies", "Is single", "Worked full-time before starting university", "Has never eaten fast food", "Has a film camera"],
    ],
    14: [
        ["Speaks 3 or more languages fluently", "Can cook", "Loves video games", "Is an only child", "Served National Service"],
        ["Prefers tea over coffee", "Is in the same faculty as you", "Shares your exact birth month", "Plays the guitar or piano", "Has worked specifically in F&B or retail"],
        ["Has a film camera", "Is terrified of cockroaches", "FREE SPACE", "Has a pet other than a dog or cat", "Is left handed"],
        ["Has never tried Mala", "Is currently learning on Duolingo", "Represented a school/national sports team", "Has a sibling who is already married", "Has an entire wardrobe that is just black and white"],
        ["Goes to the cinema at least once a month", "Is single", "Cant handle spice", "Read more than 10 books last year", "Dips their fries in ice cream"],
    ],
    15: [
        ["Plays cozy video games", "Has more than two pets at home", "Was born in a different country than they currently live in", "Needs at least two cups of coffee a day to function", "Prefers physical books over e-books"],
        ["Has dyed their hair", "Has run a half-marathon or full marathon", "Shares your exact birth month", "Sings in a choir or plays in a band", "Has worked specifically in F&B or retail"],
        ["Dips their fries in ice cream", "Has a film camera", "FREE SPACE", "Has a pet other than a dog or cat", "Is left handed"],
        ["Transferred from another university", "Dislikes bubble tea", "Doesn't drink carbonated sodas at all", "Is the eldest sibling in their family", "Has an entire wardrobe that is just black and white"],
        ["Goes to the cinema at least once a month", "Can bake a cake or cookies completely from scratch", "Cant handle spice", "Read more than 10 books last year", "Is terrified of cockroaches"],
    ],
}


# --- Accessors --------------------------------------------------------------
def is_free(row: int, col: int) -> bool:
    """True for the FREE SPACE centre cell (skipped by OCR)."""
    return row == 2 and col == 2


def prompt_for(sheet_no: int, row: int, col: int) -> str:
    """The printed prompt at (row, col) on the given sheet.
    Raises KeyError for an unknown sheet number."""
    return SHEETS[sheet_no][row][col]


def template_path(sheet_no: int) -> pathlib.Path:
    """Filesystem path to a sheet's PNG asset."""
    return TEMPLATE_DIR / f"{sheet_no}.png"
