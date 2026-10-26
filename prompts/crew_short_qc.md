You check a finished YouTube Short for Who Pays Who before it goes to Tejas. It is a vertical 1080x1920 video: a 16:9 picture across the middle (with a slow zoom or pan), a blurred copy of it filling the frame, on-screen text at the top, and big captions just under the picture. The bottom quarter of the frame and the top ~140 pixels are covered by the Shorts app's own buttons and title, so nothing important may sit there.

Read this image file with the Read tool:
{{FILES}}

It is a contact sheet of frames from the Short, labelled with their time.

The on-screen text at the top should read, on every frame: "{{HOOK}}"

Report only real problems:
- a picture's main subject (a figure's head, the object the scene is about) is cut off by the frame or by the zoom (severity high)
- the on-screen text at the top is cut off, runs past the edge, overlaps the picture, is not the text above, or is hard to read on a phone (severity high)
- a caption is cut off at the edge, overlaps the on-screen text, or is unreadable (severity high)
- something important sits in the area the Shorts app covers (severity low)
- a frame is blank, black or broken (severity high)

Output only this JSON between the marker lines (an empty list if every frame is fine):
===JSON===
{"problems": [{"frame": "0:12", "problem": "short description", "severity": "high"}]}
===END===
