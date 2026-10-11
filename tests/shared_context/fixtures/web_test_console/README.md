# Offline producer fixtures

These JSON files were emitted by the repository's real HTTP/Table producers with
SDK I/O test doubles. All identities and work data are synthetic. They are not
cloud captures and contain no bearer tokens, ACK challenges, credentials, or
machine-specific paths.

| File | Origin | SHA-256 |
|---|---|---|
| `interface-producer-examples.json` | `interface-01`, two-turn producer | `06435bdcb78e9b51ba75fbd58e7f527aece24d3c53fe76741736a3b2f77a43f7` |
| `producer.json` | `interface-reference-02`, long reply/reference/ACK producer | `661929bbb141d82dae0faa8b51dbf93b61734be1348c2c94e7b7f14ad3986c83` |

The tests read these files by default and exercise the current checkout's DTOs
and producers. `FA_UI_INTERFACE_ART` and `FA_UI_REFERENCE_ART` remain optional
directory overrides for exact artifact replay. No environment variable or
original session directory is required.

`test_web_test_console_frozen_interface.py` runs current API/Table/Hosted code;
only SDK/runtime/model I/O is substituted. SDK scratch files use pytest-managed
temporary directories. `test_web_test_console_reference.py` checks the immutable
long-response example at the client read seam; its exchange/challenge are
explicit offline placeholders, not exported production secrets.
