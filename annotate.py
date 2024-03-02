import atexit
import base64
import re
import shelve
import zipfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from io import BytesIO
from textwrap import dedent

from PIL import Image, ImageDraw
import pngquant
from openai import OpenAI

client = OpenAI()
from ebooklib import epub
from pyquery import PyQuery
from pathlib import Path
from natsort import natsorted
from pydantic import BaseModel, Field
from typing import List
from partial_json_parser import loads as partial_json_loads


base_dir = Path(__file__).parent
cache_file = base_dir / 'cache'


### caching ###

# Open the shelf
cache_db = shelve.open(str(cache_file))

def store_cache(key, value):
    cache_db[key] = value

def get_cache(key):
    return cache_db.get(key)

def cleanup_cache():
    print("Closing the database...")
    cache_db.close()

atexit.register(cleanup_cache)

def cached(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        cache_key = kwargs.pop('cache_key', None)
        if cache_key:
            if cached := get_cache(cache_key):
                return cached
        out = func(*args, **kwargs)
        if cache_key:
            store_cache(cache_key, out)
        return out
    return wrapper

### epub functions ###

def unpack_epub(epub_path, dest_dir):
    shutil.rmtree(dest_dir, ignore_errors=True)
    with zipfile.ZipFile(epub_path, 'r') as zip_ref:
        zip_ref.extractall(dest_dir)


def pack_epub(source_dir, epub_path):
    shutil.rmtree(epub_path, ignore_errors=True)
    # equivalent of `zip -rX ../my.epub mimetype META-INF/ EPUB/`
    with zipfile.ZipFile(epub_path, 'w') as zipf:
        zipf.write(source_dir / 'mimetype', 'mimetype', compress_type=zipfile.ZIP_STORED)
        for path in source_dir.glob('**/*'):
            if path.relative_to(source_dir) == Path('mimetype'):
                continue
            zipf.write(path, path.relative_to(source_dir), compress_type=zipfile.ZIP_DEFLATED)

def parse_xml(b):
    return PyQuery(b, namespaces=epub.NAMESPACES)

def serialize_xml(pq):
    return pq.outer_html(method='xml')

def read_xml(path):
    return parse_xml(path.read_bytes())

def write_xml(path, pq):
    path.write_text('<?xml version="1.0" encoding="utf-8"?>\n' + serialize_xml(pq))

### openai functions ###

def make_openai_tool(name, description, model):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.schema(),
        },
    }

def make_openai_tool_list(name, description, model):
    class ItemList(BaseModel):
        items: List[model] = Field(description="List of items")
    return make_openai_tool(name, description, ItemList)

class Dialogue(BaseModel):
    speaker: str = Field(description="The speaker in the conversation")
    line: str = Field(description="The line spoken by the speaker")

class Annotation(BaseModel):
    text: str = Field(description="Verbatim text snippet to annotate")
    annotation: str = Field(description="The annotation text to show for that snippet")


class Addition(BaseModel):
    existing_sentence: str = Field(description="Verbatim text of the existing sentence")
    new_sentence: str = Field(description="New sentence to insert afterward")

class Illustration(BaseModel):
    existing_sentence: str = Field(description="Verbatim text of the existing sentence")
    image_description: str = Field(description="Description of an illustration for the sentence")

class ReaderAnnotation(BaseModel):
    text: str = Field(description="Verbatim text snippet to annotate")
    annotation: str = Field(description="The annotation text to show for that snippet")
    reader: str = Field(description="The name of the reader supplying the annotation")

dialogue_tool = make_openai_tool_list("dialog", "Process a dialog", Dialogue)
annotate_tool = make_openai_tool_list("annotate", "Handle list of annotations", Annotation)
reader_annotate_tool = make_openai_tool_list("annotate", "Handle list of annotations", ReaderAnnotation)
addition_tool = make_openai_tool("addition", "Handle new sentence", Addition)
illustration_tool = make_openai_tool("illustration", "Generate illustration", Illustration)

@cached
def get_completion_text(prompt, tool=None, **kwargs):
    if tool:
        kwargs["tools"] = [tool]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": tool["function"]["name"]}}
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**{
        "model": "gpt-4-0125-preview",
        "max_tokens": 500,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        **kwargs,
    })
    out_list = []
    if tool:
        for choice in response.choices:
            out = partial_json_loads(choice.message.tool_calls[0].function.arguments)
            if out.keys() == {'items'}:
                out = out['items']
            out_list.append(out)
    else:
        out_list = [choice.message.content for choice in response.choices]
    out = out_list if 'n' in kwargs else out_list[0]
    return out

@cached
def get_image(prompt):
    response = client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        # size="512x512",
        quality="standard",
        n=1,
        response_format="b64_json",
    )
    return base64.b64decode(response.data[0].b64_json)

@cached
def compress_image(data):
    # resize to 50%
    img = Image.open(BytesIO(data))
    resized = img.resize((768, 768), Image.LANCZOS)
    output = BytesIO()
    resized.save(output, format="PNG")
    # compress
    compression_ratio, data = pngquant.quant_data(output.getvalue())
    return data

### threads ###

def run_threaded(func, jobs, max_workers=10):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(lambda j: func(**j), jobs))

### main ###

def process_chapter(chapter_path, title, author):
    print("Processing", chapter_path)
    out = {}
    chapter_xml = chapter_path.read_bytes()
    pq = parse_xml(chapter_xml)
    section = pq('XHTML|section')
    if not section:
        return
    text = section.text()
    section_xml = re.search(r'<section.*?>(.*)</section>', chapter_xml.decode(), re.DOTALL).group(1)
    whitespace = section[0].text if not(section[0].text.strip()) else ''

    # run API queries
    precis_prompt = dedent("""
        Write a very concise chapter precis or argument to go at the start of the above chapter, in the fashion of a 19th
        century novel. The precis should be a single sentence separated by semicolons, in abbreviated syntax rather than
        full grammar and explanation. Be explicit rather than vague; for example, instead of saying "relationship dynamics
        revealed," say what the dynamics are; instead of saying "setting the stage for future developments", say what kind
        of developments might be expected. Do not leave out any major episodes of the chapter.
        For example: "Introducing Mr. Smith, a resident of Whitehall; his interest in trains; a debate with Mr.
        Jones regarding train design; ominous signs for their upcoming presentation."
        Use a lighthearted or teasing tone that might tempt the reader into reading the next chapter.
    """).strip()
    commentary_prompt = dedent("""
        Roleplay a short scene between the following commentary bots discussing the chapter:

        * PoserBot likes to be taken very seriously about its deep insights about the text.
        * SlackerBot didn't actually bother to read the chapter and is trying to pull out a line or two to comment on.
        * SocialBot is hoping to get everyone psyched up about twists and turns in the book.
        * PoetBot is an amateur poet who comments on any particularly lyrical passages.
        * SassBot is a snarky critic who offers varied and incisive commentary.
        
        The bots should be written wittily and precisely, like prestige TV characters.
        
        Write a conversation with five or six total lines by these characters. Mix up the order they talk in.
        Make sure to have the characters speak in short, natural sentences instead of paragraphs. 
        NOT EVERYONE HAS TO TALK if they don't have something relevant, and a single character can talk more than once.
        
        Use the dialogue tool and return each line in json as {'speaker': 'name', 'line': 'text'}.
    """).strip()
    annotation_prompt = dedent("""
        Provide a few officious annotations, in the same style as the original text, as by a Publisher who seeks to
        sound knowing, sophisticated, well informed, and wry, but may be blustery or inadvertantly humorous.
        
        Use the annotate tool and return each annotation in json as
        {'text': 'verbatim text to annotate', 'annotation': 'footnote'}.
    """).strip()
    addition_prompt = dedent("""
        Propose one additional sentence, in the style of the original, that could be added to improve the text.
        Use the addition tool and return the sentence in json as {'existing_sentence': 'text', 'new_sentence': 'text'}.
    """).strip()
    illustration_prompt = dedent("""
        Extract and return one sentence from the chapter that could be illustrated by an engraving. Write a short 
        description of an image that could illustrate the sentence in the context of the chapter. Describe a realistic
        scene from the chapter rather than fantastical imagery.
        
        Use the illustration tool and return the sentence and description in json as
        {'existing_sentence': 'text', 'image_description': 'text'}.
    """).strip()

    prompt_prefix = f"The following is a chapter from {title} by {author}:\n\n{text}\n\nINSTRUCTIONS: "
    prompts = [
        {'prompt': prompt_prefix+precis_prompt, 'cache_key': f'precis_{chapter_path.stem}'},
        {'prompt': prompt_prefix+commentary_prompt, 'cache_key': f'commentary11_{chapter_path.stem}', 'tool': dialogue_tool, 'temperature': 1.1},
        {'prompt': prompt_prefix+annotation_prompt, 'cache_key': f'annotation19_{chapter_path.stem}', 'tool': annotate_tool, 'temperature': 1.2},
        {'prompt': prompt_prefix+addition_prompt, 'cache_key': f'addition9_{chapter_path.stem}', 'tool': addition_tool, 'temperature': 1.4, 'n': 5},
        {'prompt': prompt_prefix+illustration_prompt, 'cache_key': f'illustration5_{chapter_path.stem}', 'tool': illustration_tool},
    ]
    summary, commentary, annotations, additions, illustration = run_threaded(get_completion_text, prompts)

    section_append = []

    ## raw text stuff

    # annotations
    if annotations:
        footnote_number = 1
        for i, annotation in enumerate(annotations):
            if not 'text' in annotation and 'annotation' in annotation:
                continue
            text = annotation["text"].rstrip('.')  # removes occasional ellipsis
            if text in section_xml:
                annotation["reader"] = "Publisher"
                id = f'{chapter_path.stem}-note{i}'
                section_xml = section_xml.replace(text, f'{text}<a class="noteref {annotation["reader"]}" epub:type="noteref" href="#{id}"><sup>{footnote_number}</sup></a>', 1)
                footnote_number += 1
                section_append.append(f'{whitespace}<aside class="footnote" epub:type="footnote" id="{id}"><strong>{annotation["reader"]}:</strong> {annotation["annotation"]}</aside>')

    # extra sentence
    for addition in additions:
        if addition['existing_sentence'] in section_xml:
            section_xml = section_xml.replace(addition['existing_sentence'], f'{addition["existing_sentence"]} {addition["new_sentence"]}', 1)
            section_append.append(f'{whitespace}<div class="ai-addition annotation-box">The Publisher regretted the necessity to add: {addition["new_sentence"]}</div>')
            break

    # append raw text notes
    section.html(section_xml)
    for s in section_append:
        section.append(s)

    ## pyquery stuff

    # illustration
    illustration_image_prompt = f"{illustration['image_description']}. Simple black and white engraving scanned from an old book with simple, clear lines. Crosshatching into white around the edges."
    illustration_data = get_image(illustration_image_prompt, cache_key=illustration_image_prompt)
    compressed_data = compress_image(illustration_data, cache_key=illustration_image_prompt+'compressed4')
    illustration_path = chapter_path.parent.parent / f'images/illustration_{chapter_path.stem}.png'
    illustration_path.write_bytes(compressed_data)
    out['manifest'] = f'\n\t\t<item href="images/{illustration_path.name}" id="{illustration_path.name}" media-type="image/png"/>'
    middle_paragraph = PyQuery(section('p')[len(section('p'))//2])
    middle_paragraph.after(f'{whitespace}<div class="ai-illustration annotation-box">{whitespace}<img src="../images/{illustration_path.name}"/>{whitespace}<p><em>{illustration["existing_sentence"]}</em></p>{whitespace}</div>')

    # summary
    summary = summary.strip('"')
    section('header').after(f'{whitespace}<div class="ai-summary annotation-box">{summary}</div>')

    # pprint(commentary)
    commentary = [c for c in commentary if type(c) is dict and 'speaker' in c and 'line' in c]
    commentary = f'{whitespace}\t'.join(f'<p><strong class="{c["speaker"]}">{c["speaker"]}:</strong> {c["line"]}</p>' for c in commentary)
    section.append(f'{whitespace}<div class="ai-commentary annotation-box">{whitespace}\t{commentary}{whitespace}</div>')

    write_xml(chapter_path, pq)
    return out

def process_epub(epub_path, work_dir, output_epub):
    unpack_epub(epub_path, work_dir)

    # get author and title
    metadata = read_xml(work_dir / 'epub/content.opf')
    title = metadata('#title').text()
    author = metadata('#author').text()

    # process chapters
    chapter_paths = natsorted(work_dir.glob('epub/text/chapter-*.xhtml'))
    jobs = [
       {'chapter_path': c, 'title': title, 'author': author}
       for i, c in enumerate(chapter_paths)
    ]#[0:5]
    chapter_results = run_threaded(process_chapter, jobs)
    pack_epub(work_dir, output_epub)

    # add css
    css_path = work_dir / 'epub/css/local.css'
    css = css_path.read_text()
    css += """
        .annotation-box { 
            border: 2px black solid;
            padding: 1em;
            margin: 1em;
            font-style: italic;
        }
        @media (prefers-color-scheme: dark) {
            .annotation-box {
                border-color: white;
            }
        }
        .SassBot{
            color: #1a8204;
        }
        .PoetBot{
            color: #ac0808;
        }
        .SocialBot{
            color: #82046e;
        }
        .SlackerBot{
            color: #825704;
        }
        .PoserBot{
            color: #1c08ac;
        }
        @media (prefers-color-scheme: dark) {
            .SassBot{
                color: #9bea8a;
            }
            .PoetBot{
                color: #ff9a9a;
            }
            .SocialBot{
                color: #f48ae4;
            }
            .SlackerBot{
                color: #ffdc98;
            }
            .PoserBot{
                color: #aca1fd;
            }
        }
    """
    css_path.write_text(css)

    # add publisher's note
    # copy file
    (work_dir / 'epub/text/publisher-note.xhtml').write_text((epub_path.parent / 'publisher-note.xhtml').read_text())
    # add to content.opf
    pq = read_xml(work_dir / 'epub/content.opf')
    pq('OPF|manifest').prepend('\n\t\t<item id="publisher-note" href="text/publisher-note.xhtml" media-type="application/xhtml+xml"/>')
    for chapter in chapter_results:
        pq('OPF|manifest').append(chapter['manifest'])
    pq('OPF|spine').prepend('\n\t\t<itemref idref="publisher-note"/>')
    write_xml(work_dir / 'epub/content.opf', pq)
    # add to toc.ncx
    pq = read_xml(work_dir / 'epub/toc.ncx')
    pq('#navmap').prepend('<navPoint id="publisher-note" playOrder="0"><navLabel><text>Publisher Note</text></navLabel><content src="text/publisher-note.xhtml"/></navPoint>')
    write_xml(work_dir / 'epub/toc.ncx', pq)
    # add to toc.xhtml
    pq = read_xml(work_dir / 'epub/toc.xhtml')
    pq('#toc > XHTML|ol').prepend('<li><a href="text/publisher-note.xhtml">Publisher Note</a></li>')
    write_xml(work_dir / 'epub/toc.xhtml', pq)

    # add cover image subtitle
    img = Image.open(work_dir / 'epub/images/cover.jpg')
    draw = ImageDraw.Draw(img)
    draw.text((img.size[0]//2 + 100, img.size[1]-150), "Annotated & Improved", fill="white", font_size=64)
    img.save(work_dir / 'epub/images/cover.jpg')

    pack_epub(work_dir, output_epub)

# drop into pdb on any exception
def excepthook(type, value, tb):
    import traceback
    import pdb
    traceback.print_exception(type, value, tb)
    pdb.pm()
import sys
sys.excepthook = excepthook



if __name__ == '__main__':
    input_epub = base_dir / 'george-eliot_middlemarch.epub'
    output_epub = input_epub.with_name('annotated_' + input_epub.name)
    work_dir = base_dir / 'work_dir'
    before_dir = base_dir / 'before'
    unpack_epub(input_epub, before_dir)
    process_epub(input_epub, work_dir, output_epub)
    print("Done!")