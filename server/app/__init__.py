# One HTTP stack for the whole app: the Anthropic SDK (1.x) and Authlib use httpx2, the maintained
# fork of httpx. Aliasing makes `import httpx` - in our code and in libraries such as ollama -
# resolve to httpx2 too, so errors, clients and test mocks are shared. This must run before
# anything imports httpx, which is why it lives in the package's __init__.
import httpx2

httpx2.alias_httpx()
