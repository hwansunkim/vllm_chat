"""Proposed revision of wave-en (household epidemic core logic).

Changes from render_infection_en.py:
- Each step is tagged LLM / ENGINE, so the reader sees which parts are generated
  and which are deterministic.
- The symptom feedback states the information barrier: the LLM receives symptom
  text only, never infection status or probabilities.
- Step bodies use one font size, with phrases shortened to fit.
- The transmission box is a detail callout for steps 4-5 and shows the SEPIR chain
  with default durations; the floating caption moved inside it.
- A second panel shows, using the default parameters, why variable wave length
  matters: the same co-location carries a different risk depending on elapsed time.
- Each panel carries its own title, notes and legend, so the two can be cut apart
  for a poster. Outputs: the combined figure plus one file per panel.
"""
import math

from PIL import ImageFont

from render import FONT, Diagram, INK, MUTED, TEAL, PURPLE, ORANGE, BG

LLM_C = '#2864B5'
LLM_FILL = '#EDF4FE'
ENG_FILL = '#EAF6F3'
ORANGE_FILL = '#FCF0E2'
RULE = '#D5DFE9'
BETA = 0.04  # default transmission coefficient, per day (infection.py)

LOOP_H = 1175   # height of the wave-loop panel
RISK_H = 360    # height of the risk panel
GAP = 40        # space between panels in the combined figure


def pill(d, x, y, label, color, fill, width):
    d.rect(x, y, width, 28, fill, color, 14, 2)
    d.text(x + 12, y + 20, label, 15, color, True)


def step(d, x, y, number, title_lines, body_lines, color, badge_fill, tag):
    d.rect(x, y, 300, 250, 'white', RULE, 19)
    d.rect(x + 20, y + 20, 42, 38, badge_fill, badge_fill, 10, 1)
    d.text(x + 32, y + 48, str(number), 23, color, True)
    if tag == 'LLM':
        pill(d, x + 222, y + 25, 'LLM', LLM_C, LLM_FILL, 58)
    else:
        pill(d, x + 192, y + 25, 'ENGINE', TEAL, ENG_FILL, 88)
    for i, line in enumerate(title_lines):
        d.text(x + 21, y + 94 + i * 29, line, 25, color, True, max_width=258)
    for i, line in enumerate(body_lines):
        d.text(x + 21, y + 166 + i * 28, line, 19, INK, max_width=262)


def risk(minutes):
    return 1 - math.exp(-BETA * minutes / 1440)


def loop_panel(d, oy=0):
    """One wave of the household epidemic scenario, with its feedback loops."""
    d.text(70, oy + 62, 'One wave of the household epidemic scenario', 34, INK, True)
    d.text(72, oy + 100, 'Agents decide what to do; the engine decides who meets whom and who becomes infected',
           21, MUTED, max_width=1620)

    d.rect(70, oy + 128, 1620, 100, '#EEF2F8', RULE, 16)
    d.text(96, oy + 164, 'Scenario inputs', 23, INK, True)
    d.text(96, oy + 196, 'Household personas and relationships · Rooms, zones, exterior spaces · Daily schedules',
           20, MUTED, max_width=1568)
    d.text(96, oy + 220, 'Patient zero and start state (E, P or I) · β and E/P/I duration distributions · Symptom texts per state',
           20, MUTED, max_width=1568)

    # symptom feedback across the top: step 5 -> step 1, with the information barrier
    y0 = oy + 366
    d.arrow([(1540, y0), (1540, oy + 286), (220, oy + 286), (220, y0)], ORANGE)
    d.rect(330, oy + 268, 1100, 36, BG, BG, 7, 1)
    d.text(345, oy + 294, 'Symptom text only: infection status and probabilities never reach the LLM',
           22, ORANGE, True, max_width=1070)

    xs = [70, 400, 730, 1060, 1390]
    step(d, xs[0], y0, 1, ['Context', 'perception'],
         ['Persona and memory', 'Time, place, who is here', 'Symptom text, if any'], LLM_C, LLM_FILL, 'ENGINE')
    step(d, xs[1], y0, 2, ['Behaviour', 'generation'],
         ['Speech and addressees', 'move_to, bring_along', 'Sleep, busy, appearance'], LLM_C, LLM_FILL, 'LLM')
    step(d, xs[2], y0, 3, ['Action', 'resolution'],
         ['Route speech (pre-move)', 'Move one hop per wave', 'Apply states, appearance'], TEAL, ENG_FILL, 'ENGINE')
    step(d, xs[3], y0, 4, ['Contact', 'assessment'],
         ['Group by place node', 'Exterior spaces excluded', 'Carriers (P, I) × S'], TEAL, ENG_FILL, 'ENGINE')
    step(d, xs[4], y0, 5, ['Infection and', 'progression'],
         ['One draw per pair', 'Advance E→P→I→R', 'On the clock, not the LLM'], ORANGE, ORANGE_FILL, 'ENGINE')
    ym = y0 + 125
    for x in xs[:-1]:
        d.arrow([(x + 300, ym), (x + 330, ym)], TEAL)

    # bottom row
    ys = y0 + 250                 # bottom edge of the step row
    yb, hb = ys + 80, 240
    d.box(70, yb, 420, hb, 'Experience and memory',
          ['Store what was said, seen, done', 'Compress into episodes and facts', 'Recent vivid, older faded'],
          color=PURPLE, fill='#F6F1FC', title_size=25)
    d.arrow([(550, ys), (550, ys + 40), (280, ys + 40), (280, yb)], PURPLE,
            label='New experiences', label_at=(318, ys + 30))
    d.arrow([(70, yb + 120), (46, yb + 120), (46, y0 + 150), (70, y0 + 150)], PURPLE)

    d.box(520, yb, 470, hb, 'Advance simulated time',
          ['LLM estimates the scene length', 'Engine caps the jump (45 / 180 min)', 'Never skips a scheduled event'],
          color=TEAL, fill=ENG_FILL, title_size=25)
    pill(d, 822, yb + 178, 'LLM + ENGINE', TEAL, 'white', 144)
    d.arrow([(880, ys), (880, yb)], TEAL, label='Scene content', label_at=(892, ys + 50))

    # transmission detail (callout for steps 4-5)
    dx, dw = 1020, 670
    d.rect(dx, yb, dw, hb, '#FFF6EB', '#EBD8BF', 16)
    d.text(dx + 24, yb + 38, 'Detail of steps 4–5', 24, ORANGE, True)
    d.text(dx + 24, yb + 80, 'p = 1 - exp(-β · Δτ)', 29, INK, True)
    d.text(dx + 340, yb + 78, 'β = 0.04 per day (default)', 19, MUTED)
    d.text(dx + 24, yb + 112, 'Δτ: time since the previous transmission check', 19, MUTED, max_width=dw - 48)
    states = [('S', False), ('E', False), ('P', True), ('I', True), ('R', False)]
    px0, pw, gap = dx + 24, 56, 72
    for i, (s, infectious) in enumerate(states):
        x = px0 + i * (pw + gap)
        d.rect(x, yb + 135, pw, 36, ORANGE if infectious else 'white', ORANGE, 10, 2)
        d.text(x + 20, yb + 160, s, 20, 'white' if infectious else ORANGE, True)
        if i < len(states) - 1:
            d.arrow([(x + pw + 6, yb + 153), (x + pw + gap - 6, yb + 153)], ORANGE)
    under = [(1, 'Gamma, 1–10 d'), (2, '0 d (skipped)'), (3, '8 d'), (4, 'immune or → S')]
    font = ImageFont.truetype(str(FONT), 15)
    for i, label in under:
        cx = px0 + i * (pw + gap) + pw / 2
        d.text(round(cx - font.getlength(label) / 2), yb + 196, label, 15, MUTED)
    d.text(dx + 24, yb + 226, 'Filled: transmits · durations drawn once, at exposure', 16, MUTED)
    d.arrow([(990, yb + 120), (1020, yb + 120)], TEAL, dashed=True)
    d.text(994, yb + 110, 'Δτ', 18, TEAL)
    d.arrow([(1540, yb), (1540, ys)], ORANGE, dashed=True)

    # next wave loop
    yl = yb + hb + 50
    d.arrow([(755, yb + hb), (755, yl), (20, yl), (20, y0 + 45), (70, y0 + 45)], TEAL)
    d.rect(200, yl - 18, 520, 36, BG, BG, 7, 1)
    d.text(214, yl + 8, 'Next wave: addressed agents, updated clock', 21, TEAL)

    # records + legend (belong to this panel)
    yr = yl + 40
    d.rect(70, yr, 1620, 58, '#EEF2F8', RULE, 14)
    d.text(96, yr + 37, 'Logged each wave', 21, INK, True)
    d.text(300, yr + 37, 'Clock · Location (CSV for contact tracing) · Speech and actions · Infection transitions',
           21, MUTED, max_width=1370)
    yg = yr + 84
    pill(d, 70, yg, 'LLM', LLM_C, LLM_FILL, 58)
    d.text(140, yg + 21, 'generated by the language model', 18, MUTED)
    pill(d, 440, yg, 'ENGINE', TEAL, ENG_FILL, 88)
    d.text(540, yg + 21, 'deterministic rule', 18, MUTED)
    d.text(760, yg + 21, 'Dashed: detail or time input  ·  Solid: flow within and between waves', 18, MUTED)
    assert yg + 28 <= oy + LOOP_H


def risk_panel(d, oy=0):
    """Per-pair infection risk for the same co-location under different wave lengths."""
    d.text(70, oy + 62, 'Same co-location, different risk', 34, INK, True)
    d.text(72, oy + 100, 'The time agents spend together sets the exposure Δτ, so wave length changes the infection risk',
           21, MUTED, max_width=1620)
    cases = [('Brief exchange', 10, 'Δτ = 10 min'), ('Shared meal', 30, 'Δτ = 30 min'),
             ('Asleep in the same room', 420, 'Δτ = 7 h')]
    for i, (name, minutes, dt) in enumerate(cases):
        x = 70 + i * 555
        d.rect(x, oy + 128, 510, 130, 'white', '#EBD8BF', 16)
        d.text(x + 24, oy + 166, name, 22, INK, True)
        d.text(x + 24, oy + 202, dt, 20, MUTED)
        d.text(x + 24, oy + 242, f'p = {risk(minutes) * 100:.2f}%', 28, ORANGE, True)
    full = 1 - math.exp(-BETA * 8)
    d.text(70, oy + 296, f'Continuous co-location for the whole 8-day infectious period: p = {full * 100:.0f}%',
           19, MUTED)
    d.text(70, oy + 326, 'Illustrative: one infectious and one susceptible agent in the same room; '
           'p = 1 - exp(-β · Δτ) with the default β = 0.04 per day', 18, MUTED, max_width=1620)


LOOP_DESC = ('Five steps of a wave in the household epidemic scenario, tagged LLM or engine, with memory, '
             'time and symptom feedback, and a detail of transmission and SEPIR progression.')
RISK_DESC = 'Per-pair infection probability for the same co-location under different wave lengths, using default parameters.'


def render():
    combined = Diagram(1800, LOOP_H + GAP + RISK_H, 'Household epidemic scenario: wave loop and exposure risk',
                       LOOP_DESC + ' ' + RISK_DESC)
    loop_panel(combined)
    combined.rect(70, LOOP_H + GAP // 2 - 1, 1620, 2, RULE, RULE, 1, 1)
    risk_panel(combined, LOOP_H + GAP)
    combined.save('wave-en-v2')

    loop_only = Diagram(1800, LOOP_H, 'One wave of the household epidemic scenario', LOOP_DESC)
    loop_panel(loop_only)
    loop_only.save('wave-en-v2-loop')

    risk_only = Diagram(1800, RISK_H, 'Same co-location, different risk', RISK_DESC)
    risk_panel(risk_only)
    risk_only.save('wave-en-v2-risk')


if __name__ == '__main__':
    render()
    print('Rendered wave-en-v2, wave-en-v2-loop and wave-en-v2-risk (svg + png)')
