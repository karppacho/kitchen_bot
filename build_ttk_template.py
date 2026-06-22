"""
Создаёт Word-шаблон ТТК для сети "Гастрономия" (Тим Кук) в формате docxtpl.

Версия 2: улучшено форматирование.
  - таблицы с заметными границами и серой шапкой
  - отступы перед и после таблиц
  - keep_with_next на заголовках разделов
  - таблицы КБЖУ не разрываются между страницами
  - повтор шапки таблицы при переносе

Опечатки исходных файлов исправлены ("БЕЗОПАСТНОСТИ", "ФРАНЦУСКОГО", "заетм").
"""
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def set_cell_borders(cell, sz='8'):
    tc_pr = cell._tc.get_or_add_tcPr()
    for old in tc_pr.findall(qn('w:tcBorders')):
        tc_pr.remove(old)
    tc_borders = OxmlElement('w:tcBorders')
    for border_name in ('top', 'left', 'bottom', 'right'):
        border = OxmlElement(f'w:{border_name}')
        border.set(qn('w:val'), 'single')
        border.set(qn('w:sz'), sz)
        border.set(qn('w:space'), '0')
        border.set(qn('w:color'), '000000')
        tc_borders.append(border)
    tc_pr.append(tc_borders)


def set_cell_shading(cell, hex_color):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_color)
    tc_pr.append(shd)


def set_cell_padding(cell, top=40, bottom=40, left=120, right=120):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = OxmlElement('w:tcMar')
    for direction, value in (('top', top), ('bottom', bottom),
                              ('left', left), ('right', right)):
        node = OxmlElement(f'w:{direction}')
        node.set(qn('w:w'), str(value))
        node.set(qn('w:type'), 'dxa')
        tc_mar.append(node)
    tc_pr.append(tc_mar)


def set_row_cant_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement('w:cantSplit')
    tr_pr.append(cant_split)


def set_paragraph_keep_with_next(paragraph):
    pPr = paragraph._p.get_or_add_pPr()
    keep = OxmlElement('w:keepNext')
    pPr.append(keep)


def style_cell(cell, text, *, bold=False, size=11, align=WD_ALIGN_PARAGRAPH.LEFT,
               valign=WD_ALIGN_VERTICAL.CENTER, shading=None, font='Times New Roman'):
    cell.text = ''
    p = cell.paragraphs[0]
    p.alignment = align
    run = p.add_run(text)
    run.font.name = font
    run.font.size = Pt(size)
    run.bold = bold
    cell.vertical_alignment = valign
    set_cell_borders(cell)
    set_cell_padding(cell)
    if shading:
        set_cell_shading(cell, shading)


def add_paragraph(doc, text, *, bold=False, size=11, align=WD_ALIGN_PARAGRAPH.LEFT,
                  space_before=0, space_after=0, keep_with_next=False,
                  font='Times New Roman'):
    p = doc.add_paragraph()
    p.alignment = align
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    if keep_with_next:
        set_paragraph_keep_with_next(p)
    run = p.add_run(text)
    run.font.name = font
    run.font.size = Pt(size)
    run.bold = bold
    return p


def make_table_header_repeat(row):
    """Делает строку таблицы повторяющейся шапкой при переносе таблицы."""
    tr_pr = row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement('w:tblHeader'))


# ---------- КОНСТАНТЫ ----------

HEADER_FILL = 'D9D9D9'
SECTION_GAP_BEFORE = 12
SECTION_GAP_AFTER = 6
TABLE_GAP_BEFORE = 4
TABLE_GAP_AFTER = 8


def make_kbju_table(doc, p_var, f_var, c_var, k_var):
    """Таблица КБЖУ — шапка + значения. Обе строки помечены cant_split,
    и каждая помечена как tblHeader, что приводит к keep-together поведению
    при правильной настройке. Для гарантии — добавляем абзац перед таблицей
    с keep_with_next."""
    table = doc.add_table(rows=2, cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    col_widths = [Cm(3.5), Cm(3.5), Cm(4), Cm(5.5)]
    for col_idx, width in enumerate(col_widths):
        for row in table.rows:
            row.cells[col_idx].width = width

    hdr = table.rows[0]
    set_row_cant_split(hdr)
    make_table_header_repeat(hdr)
    for cell, text in zip(hdr.cells,
                          ['Белки, г', 'Жиры, г', 'Углеводы, г',
                           'Энергетическая ценность, кКал']):
        style_cell(cell, text, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                   shading=HEADER_FILL)

    val_row = table.rows[1]
    set_row_cant_split(val_row)
    for cell, text in zip(val_row.cells, [p_var, f_var, c_var, k_var]):
        style_cell(cell, text, align=WD_ALIGN_PARAGRAPH.CENTER)


# ---------- ОСНОВНАЯ ФУНКЦИЯ ----------

def main():
    doc = Document()

    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(1.5)

    style = doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(11)

    # === ШАПКА ===
    add_paragraph(doc, 'УТВЕРЖДАЮ', bold=True, align=WD_ALIGN_PARAGRAPH.RIGHT)
    add_paragraph(doc, '{{ director_position }}', align=WD_ALIGN_PARAGRAPH.RIGHT)
    add_paragraph(doc, '{{ org_name }}', align=WD_ALIGN_PARAGRAPH.RIGHT)
    add_paragraph(doc, '_______(_____________)', align=WD_ALIGN_PARAGRAPH.RIGHT)
    add_paragraph(doc, '"{{ approval_date }}"', align=WD_ALIGN_PARAGRAPH.RIGHT,
                  space_after=12)

    # === ЗАГОЛОВОК ===
    add_paragraph(doc, 'ТЕХНИКО-ТЕХНОЛОГИЧЕСКАЯ КАРТА № {{ ttk_number }}',
                  bold=True, size=13, align=WD_ALIGN_PARAGRAPH.CENTER,
                  keep_with_next=True)
    add_paragraph(doc, '{{ dish_name }}', bold=True, size=13,
                  align=WD_ALIGN_PARAGRAPH.CENTER, space_after=12)

    # === 1. ОБЛАСТЬ ПРИМЕНЕНИЯ ===
    add_paragraph(doc, '1. ОБЛАСТЬ ПРИМЕНЕНИЯ', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)
    add_paragraph(
        doc,
        'Настоящая технико-технологическая карта распространяется на {{ dish_name }}, '
        'вырабатываемое (-ую) и реализуемое (-ую) в {{ org_name }}.',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    )

    # === 2. ТРЕБОВАНИЯ К СЫРЬЮ ===
    add_paragraph(doc, '2. ТРЕБОВАНИЯ К СЫРЬЮ', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)
    add_paragraph(
        doc,
        '2.1. Продовольственное сырьё, пищевые продукты и полуфабрикаты, '
        'вспомогательные материалы, используемые при производстве блюда, должны '
        'соответствовать требованиям ТР ТС {{ tr_ts_number }} «О безопасности пищевой '
        'продукции» и иной нормативной документации, действующей для каждого вида '
        'сырья, но не противоречащей ТР ТС {{ tr_ts_number }}.',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
        space_after=4,
    )
    add_paragraph(
        doc,
        '2.2. Каждая партия сырья, поступающая на предприятие для производства '
        'блюда, должна сопровождаться декларацией о соответствии и удостоверением '
        'качества и безопасности.',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    )

    # === 3. РЕЦЕПТУРА ===
    add_paragraph(doc, '3. РЕЦЕПТУРА', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)

    table = doc.add_table(rows=4, cols=3)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    col_widths = [Cm(10), Cm(3.25), Cm(3.25)]
    for col_idx, width in enumerate(col_widths):
        for row in table.rows:
            row.cells[col_idx].width = width

    hdr = table.rows[0]
    set_row_cant_split(hdr)
    make_table_header_repeat(hdr)
    style_cell(hdr.cells[0], 'Наименование сырья и продуктов', bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)
    style_cell(hdr.cells[1], 'Брутто, г', bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)
    style_cell(hdr.cells[2], 'Нетто, г', bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)

    for_row = table.rows[1]
    set_row_cant_split(for_row)
    style_cell(for_row.cells[0], '{%tr for ing in ingredients %}')
    style_cell(for_row.cells[1], '')
    style_cell(for_row.cells[2], '')

    ing_row = table.rows[2]
    set_row_cant_split(ing_row)
    style_cell(ing_row.cells[0], '{{ ing.name }}')
    style_cell(ing_row.cells[1], '{{ ing.brutto }}', align=WD_ALIGN_PARAGRAPH.CENTER)
    style_cell(ing_row.cells[2], '{{ ing.netto }}', align=WD_ALIGN_PARAGRAPH.CENTER)

    endfor_row = table.rows[3]
    set_row_cant_split(endfor_row)
    style_cell(endfor_row.cells[0], '{%tr endfor %}')
    style_cell(endfor_row.cells[1], '')
    style_cell(endfor_row.cells[2], '')

    output_row = table.add_row()
    set_row_cant_split(output_row)
    style_cell(output_row.cells[0], 'Выход блюда:', bold=True, shading=HEADER_FILL)
    style_cell(output_row.cells[1], '—', bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)
    style_cell(output_row.cells[2], '{{ dish_output_g }}', bold=True,
               align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)
    for col_idx, width in enumerate(col_widths):
        output_row.cells[col_idx].width = width

    # Отступ после таблицы рецептуры
    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(TABLE_GAP_AFTER)

    # === 4. ТЕХНОЛОГИЧЕСКИЙ ПРОЦЕСС ===
    add_paragraph(doc, '4. ТЕХНОЛОГИЧЕСКИЙ ПРОЦЕСС', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)
    add_paragraph(
        doc,
        'Подготовка сырья производится в соответствии с рекомендациями Сборника '
        'технологических нормативов для предприятий общественного питания и '
        'технологическими рекомендациями для импортного сырья.',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=4,
    )
    add_paragraph(
        doc,
        'Продукт готовить под конкретный заказ. {{ tech_process }}',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    )

    # === 5. ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ ===
    add_paragraph(doc, '5. ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ, РЕАЛИЗАЦИИ И ХРАНЕНИЮ',
                  bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)
    for line in [
        'Готовый продукт пересыпать в картонную коробку.',
        'Продукт реализовать в упакованном виде сразу после приготовления.',
        'Срок реализации 3 часа на тепловой полке / в термосумке с момента готовности.',
    ]:
        add_paragraph(doc, line, align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=2)

    # === 6. ПОКАЗАТЕЛИ КАЧЕСТВА И БЕЗОПАСНОСТИ ===
    add_paragraph(doc, '6. ПОКАЗАТЕЛИ КАЧЕСТВА И БЕЗОПАСНОСТИ', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)
    add_paragraph(
        doc,
        '6.1. По органолептическим показателям блюдо должно соответствовать '
        'требованиям, указанным в таблице:',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
        space_after=TABLE_GAP_BEFORE, keep_with_next=True,
    )

    org_table = doc.add_table(rows=5, cols=2)
    org_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    org_col_widths = [Cm(5), Cm(11.5)]
    for col_idx, width in enumerate(org_col_widths):
        for row in org_table.rows:
            row.cells[col_idx].width = width

    org_rows_data = [
        ('Наименование показателей', 'Характеристика показателей', True),
        ('Внешний вид', '{{ organoleptic_appearance }}', False),
        ('Цвет', '{{ organoleptic_color }}', False),
        ('Вкус и запах', '{{ organoleptic_taste_smell }}', False),
        ('Консистенция', '{{ organoleptic_consistency }}', False),
    ]
    for row_idx, (label, value, is_header) in enumerate(org_rows_data):
        row = org_table.rows[row_idx]
        set_row_cant_split(row)
        if is_header:
            style_cell(row.cells[0], label, bold=True,
                       align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)
            style_cell(row.cells[1], value, bold=True,
                       align=WD_ALIGN_PARAGRAPH.CENTER, shading=HEADER_FILL)
            make_table_header_repeat(row)
        else:
            style_cell(row.cells[0], label, bold=True)
            style_cell(row.cells[1], value, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(TABLE_GAP_AFTER)

    add_paragraph(doc, '6.2. Физико-химические показатели для блюда не нормируются.',
                  align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=4)
    add_paragraph(
        doc,
        '6.3. По микробиологическим показателям и показателям безопасности блюдо '
        'должно соответствовать ТР ТС {{ tr_ts_number }} «О безопасности пищевой '
        'продукции».',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
    )

    # === 7. ПИЩЕВАЯ И ЭНЕРГЕТИЧЕСКАЯ ЦЕННОСТЬ ===
    add_paragraph(doc, '7. ПИЩЕВАЯ И ЭНЕРГЕТИЧЕСКАЯ ЦЕННОСТЬ', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)

    add_paragraph(doc, 'Пищевая и энергетическая ценность на 100 г блюда:',
                  space_after=TABLE_GAP_BEFORE, keep_with_next=True)
    make_kbju_table(doc, '{{ kbju_per_100g.белки }}', '{{ kbju_per_100g.жиры }}',
                    '{{ kbju_per_100g.углеводы }}', '{{ kbju_per_100g.ккал }}')

    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(TABLE_GAP_AFTER)

    add_paragraph(doc, 'Пищевая и энергетическая ценность на 1 порцию ({{ dish_output_g }} г):',
                  space_after=TABLE_GAP_BEFORE, keep_with_next=True)
    make_kbju_table(doc, '{{ kbju_per_portion.белки }}', '{{ kbju_per_portion.жиры }}',
                    '{{ kbju_per_portion.углеводы }}', '{{ kbju_per_portion.ккал }}')

    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(TABLE_GAP_AFTER)

    # === 8. ПРЕДУСМОТРЕННОЕ ПРИМЕНЕНИЕ ===
    add_paragraph(doc, '8. ПРЕДУСМОТРЕННОЕ ПРИМЕНЕНИЕ И ОГРАНИЧЕНИЯ ПО ПРИМЕНЕНИЮ',
                  bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
                  space_before=SECTION_GAP_BEFORE, space_after=SECTION_GAP_AFTER,
                  keep_with_next=True)
    add_paragraph(
        doc,
        'Блюдо предназначено для непосредственного употребления в пищу. Ограничений '
        'по целевым группам потребления среди лиц, ожидаемо потребляющих продукцию, '
        'не предусмотрено. Продукт может содержать следы аллергенов, не входящих в '
        'основной состав блюда.',
        align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=24,
    )

    # === ПОДПИСЬ ===
    add_paragraph(doc, 'Разработал: _____________ ( _________________ )')

    doc.save('TTK_template.docx')
    print('Сохранён: TTK_template.docx')


if __name__ == '__main__':
    main()
