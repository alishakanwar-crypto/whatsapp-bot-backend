import cv2
import numpy as np

from app.routes.answer_check import decode_image, find_words, place_errors


def _page():
    im = np.full((700, 1000, 3), 245, np.uint8)
    for y in range(120, 700, 90):
        cv2.line(im, (0, y), (1000, y), (210, 190, 160), 2)
    x = 60
    for w in ["I", "goes", "to", "scool"]:
        cv2.putText(im, w, (x, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (90, 30, 20), 3)
        x += 60 + 38 * len(w)
    return im


def test_find_words_boxes_each_word_in_reading_order():
    boxes = find_words(_page())
    assert len(boxes) == 4
    assert [b[0] for b in boxes] == sorted(b[0] for b in boxes)


def test_place_errors_moves_a_wrong_id_to_the_matching_word():
    boxes = [(10, 10, 20, 20), (40, 10, 50, 20), (100, 10, 20, 20), (130, 10, 60, 20)]
    raw = {
        "words": [[1, "I"], [2, "goes"], [3, "to"], [4, "scool."]],
        "errors": [
            {"ids": [3], "wrong": "scool", "correct": "school", "type": "spelling", "why": "x"},
            {"ids": [2], "wrong": "goes", "correct": "go", "type": "Grammar|Usage", "why": "y"},
            {"ids": [2], "wrong": "goes", "correct": "go", "type": "Grammar", "why": "dup"},
            {"ids": [99], "wrong": "", "correct": ".", "type": "odd", "why": "z"},
        ],
    }
    out = place_errors(raw, boxes, 200, 40)
    assert len(out) == 3
    by = {e["correct"]: e for e in out}
    assert by["school"]["box"] == [0.65, 0.25, 0.3, 0.5]
    assert by["school"]["type"] == "Spelling"
    assert by["go"]["type"] == "Grammar"
    assert by["."]["box"] is None and by["."]["type"] == "Grammar"


def test_place_errors_keeps_a_phrase_on_a_box_holding_the_whole_phrase():
    boxes = [(10, 10, 20, 20), (40, 10, 80, 20), (130, 10, 30, 20)]
    raw = {"words": [[1, "I"], [2, "have enjoyed"], [3, "have"]],
           "errors": [{"ids": [2], "wrong": "have enjoyed", "correct": "enjoyed", "type": "Grammar", "why": "x"}]}
    assert place_errors(raw, boxes, 200, 40)[0]["box"][0] == 0.2


def test_place_errors_keeps_two_punctuation_fixes_on_one_word():
    boxes = [(10, 10, 40, 20)]
    raw = {"words": [[1, "Why"]], "errors": [
        {"ids": [1], "wrong": "", "correct": "?", "type": "Punctuation", "why": "q"},
        {"ids": [1], "wrong": "", "correct": '"', "type": "Punctuation", "why": "quote"}]}
    assert len(place_errors(raw, boxes, 200, 40)) == 2


def test_decode_image_rejects_huge_dimensions():
    import base64
    import io

    from PIL import Image
    b = io.BytesIO()
    Image.new("L", (9000, 9000), 255).save(b, "PNG")
    assert decode_image(base64.b64encode(b.getvalue()).decode()) is None
    b = io.BytesIO()
    Image.new("RGB", (3000, 1000), "white").save(b, "JPEG")
    assert decode_image(base64.b64encode(b.getvalue()).decode()).shape[:2] == (667, 2000)


def test_place_errors_drops_style_tips_and_marks_already_present():
    boxes = [(10, 10, 40, 20), (60, 10, 40, 20)]
    raw = {"words": [[1, "parents."], [2, "like"]], "errors": [
        {"ids": [1], "wrong": "", "correct": ".", "type": "Punctuation", "why": "Sentence needs a full stop."},
        {"ids": [2], "wrong": "like", "correct": "such as", "type": "Usage", "why": "Examples are introduced better here."}]}
    assert place_errors(raw, boxes, 200, 40) == []
