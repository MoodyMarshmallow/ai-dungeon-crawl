import Prism from "prismjs";
import "prismjs/components/prism-python";
import "prismjs/components/prism-json";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-typescript";
import MarkdownIt from "markdown-it";

Prism.manual = true;

/** Reject terminal controls from model text; only our renderer may emit ANSI. */
export function terminalText(value: string): string {
  return value
    .replace(/\r\n/g, "\n")
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "");
}

export function codeHtml(source: string, language = "bash"): string {
  const aliases: Record<string, string> = {
    py: "python",
    js: "javascript",
    ts: "typescript",
    sh: "bash",
  };
  const name = aliases[language] ?? language;
  const grammar = Object.hasOwn(Prism.languages, name)
    ? Prism.languages[name]
    : undefined;
  return grammar && typeof grammar === "object"
    ? Prism.highlight(source, grammar, name)
    : (Prism.util.encode(source) as string);
}

const markdown = new MarkdownIt({
  html: false,
  linkify: false,
  highlight: codeHtml,
});
// No remote images, embedded HTML, or automatic network requests from model output.
markdown.disable("image");
markdown.renderer.rules.link_open = (
  tokens,
  index,
  options,
  _env,
  renderer,
) => {
  tokens[index]!.attrSet("target", "_blank");
  tokens[index]!.attrSet("rel", "noopener noreferrer");
  return renderer.renderToken(tokens, index, options);
};

export function markdownHtml(source: string): string {
  return markdown.render(source);
}

const colors: Record<string, string> = {
  keyword: "198;165;222",
  boolean: "198;165;222",
  string: "167;196;150",
  comment: "145;145;145",
  number: "220;178;132",
  function: "147;187;216",
  builtin: "147;187;216",
};

/** Use Prism's token tree for terminal colors, never ANSI supplied by the model. */
export function pythonAnsi(source: string): string {
  function paint(
    value: string | Prism.Token | (string | Prism.Token)[],
    inherited = "",
  ): string {
    if (typeof value === "string") {
      return inherited ? `\x1b[38;2;${inherited}m${value}\x1b[39m` : value;
    }
    if (Array.isArray(value))
      return value.map((item) => paint(item, inherited)).join("");
    return paint(value.content, colors[value.type] ?? inherited);
  }
  return paint(Prism.tokenize(terminalText(source), Prism.languages.python!));
}

/** Highlight shell commands for the terminal transcript. */
export function shellAnsi(source: string): string {
  function paint(
    value: string | Prism.Token | (string | Prism.Token)[],
    inherited = "",
  ): string {
    if (typeof value === "string")
      return inherited ? `\x1b[38;2;${inherited}m${value}\x1b[39m` : value;
    if (Array.isArray(value))
      return value.map((item) => paint(item, inherited)).join("");
    return paint(value.content, colors[value.type] ?? inherited);
  }
  return paint(Prism.tokenize(terminalText(source), Prism.languages.bash!));
}
