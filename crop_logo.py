from PIL import Image, ImageChops

img = Image.open("static/images/karacel-logo.png").convert("RGBA")

# Find bounding box of non-white content
bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
diff = ImageChops.difference(img, bg)
bbox = diff.getbbox()

if bbox:
    cropped = img.crop(bbox)
    cropped.save("static/images/karacel-logo.png")
    print(f"Cropped to {cropped.size}")
else:
    print("No whitespace detected.")