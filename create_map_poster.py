import os
import json
import time
import argparse
from datetime import datetime
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

import osmnx as ox
import numpy as np
from geopy.geocoders import Nominatim
from tqdm import tqdm

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
import matplotlib.colors as mcolors

# Optimize matplotlib for low memory
plt.ioff()  # Turn off interactive mode
import gc
gc.enable()  # Enable garbage collection

# Configure OSMnx with memory optimizations
APP_UA = os.getenv("OSM_USER_AGENT", "CityMapPoster/1.0 (contact: bo.hamilton09@gmail.com)")
ox.settings.http_headers = {"User-Agent": APP_UA}
ox.settings.use_cache = True
ox.settings.cache_folder = "./cache"
ox.settings.memory_only_mode = True  # Don't write graphs to disk
ox.settings.log_console = False  # Reduce logging overhead

THEMES_DIR = "themes"
FONTS_DIR = "fonts"
POSTERS_DIR = "posters"
POSTER_SIZES = {
    "small": {"label": "Small", "inches": (11, 17), "use_case": "Handouts, clipboards"},
    "medium": {"label": "Medium", "inches": (18, 24), "use_case": "Office / hallway"},
    "large": {"label": "Large", "inches": (24, 36), "use_case": "Wall posters"},
    "xl": {"label": "XL", "inches": (36, 48), "use_case": "Trade shows, lobbies"},
}

def load_fonts():
    """
    Load Roboto fonts from the fonts directory.
    Returns dict with font paths for different weights.
    """
    fonts = {
        'bold': os.path.join(FONTS_DIR, 'Roboto-Bold.ttf'),
        'regular': os.path.join(FONTS_DIR, 'Roboto-Regular.ttf'),
        'light': os.path.join(FONTS_DIR, 'Roboto-Light.ttf')
    }
    
    # Verify fonts exist
    for weight, path in fonts.items():
        if not os.path.exists(path):
            print(f"⚠ Font not found: {path}")
            return None
    
    return fonts

FONTS = load_fonts()

def generate_output_filename(city, theme_name):
    """
    Generate unique output filename with city, theme, and datetime.
    """
    if not os.path.exists(POSTERS_DIR):
        os.makedirs(POSTERS_DIR)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    city_slug = city.lower().replace(' ', '_')
    filename = f"{city_slug}_{theme_name}_{timestamp}.png"
    return os.path.join(POSTERS_DIR, filename)

def get_available_themes():
    """
    Scans the themes directory and returns a list of available theme names.
    """
    if not os.path.exists(THEMES_DIR):
        os.makedirs(THEMES_DIR)
        return []
    
    themes = []
    for file in sorted(os.listdir(THEMES_DIR)):
        if file.endswith('.json'):
            theme_name = file[:-5]  # Remove .json extension
            themes.append(theme_name)
    return themes

AVAILABLE_THEMES = get_available_themes()

@lru_cache(maxsize=32)
def load_theme(theme_name="feature_based"):
    """
    Load theme from JSON file in themes directory.
    """
    theme_file = os.path.join(THEMES_DIR, f"{theme_name}.json")
    
    if not os.path.exists(theme_file):
        print(f"⚠ Theme file '{theme_file}' not found. Using default feature_based theme.")
        # Fallback to embedded default theme
        return {
            "name": "Feature-Based Shading",
            "bg": "#FFFFFF",
            "text": "#000000",
            "gradient_color": "#FFFFFF",
            "water": "#C0C0C0",
            "parks": "#F0F0F0",
            "road_motorway": "#0A0A0A",
            "road_primary": "#1A1A1A",
            "road_secondary": "#2A2A2A",
            "road_tertiary": "#3A3A3A",
            "road_residential": "#4A4A4A",
            "road_default": "#3A3A3A"
        }
    
    with open(theme_file, 'r') as f:
        theme = json.load(f)
        print(f"✓ Loaded theme: {theme.get('name', theme_name)}")
        if 'description' in theme:
            print(f"  {theme['description']}")
        return theme

# Load theme (can be changed via command line or input)
THEME = None  # Will be loaded later

def create_gradient_fade(ax, color, location='bottom', zorder=10):
    """
    Creates a fade effect at the top or bottom of the map.
    """
    vals = np.linspace(0, 1, 256).reshape(-1, 1)
    gradient = np.hstack((vals, vals))
    
    rgb = mcolors.to_rgb(color)
    my_colors = np.zeros((256, 4))
    my_colors[:, 0] = rgb[0]
    my_colors[:, 1] = rgb[1]
    my_colors[:, 2] = rgb[2]
    
    if location == 'bottom':
        my_colors[:, 3] = np.linspace(1, 0, 256)
        extent_y_start = 0
        extent_y_end = 0.25
    else:
        my_colors[:, 3] = np.linspace(0, 1, 256)
        extent_y_start = 0.75
        extent_y_end = 1.0

    custom_cmap = mcolors.ListedColormap(my_colors)
    
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    y_range = ylim[1] - ylim[0]
    
    y_bottom = ylim[0] + y_range * extent_y_start
    y_top = ylim[0] + y_range * extent_y_end
    
    ax.imshow(gradient, extent=[xlim[0], xlim[1], y_bottom, y_top], 
              aspect='auto', cmap=custom_cmap, zorder=zorder, origin='lower')

def get_edge_colors_and_widths_by_type(G):
    """
    Assigns colors and widths to edges based on road type hierarchy.
    Returns tuple of (colors, widths) corresponding to each edge in the graph.
    Combined single-pass iteration for better performance.
    """
    edge_colors = []
    edge_widths = []

    for u, v, data in G.edges(data=True):
        # Get the highway type (can be a list or string)
        highway = data.get('highway', 'unclassified')

        # Handle list of highway types (take the first one)
        if isinstance(highway, list):
            highway = highway[0] if highway else 'unclassified'

        # Assign color and width based on road type (single pass)
        if highway in ['motorway', 'motorway_link']:
            color = THEME['road_motorway']
            width = 1.2
        elif highway in ['trunk', 'trunk_link', 'primary', 'primary_link']:
            color = THEME['road_primary']
            width = 1.0
        elif highway in ['secondary', 'secondary_link']:
            color = THEME['road_secondary']
            width = 0.8
        elif highway in ['tertiary', 'tertiary_link']:
            color = THEME['road_tertiary']
            width = 0.6
        elif highway in ['residential', 'living_street', 'unclassified']:
            color = THEME['road_residential']
            width = 0.4
        else:
            color = THEME['road_default']
            width = 0.4

        edge_colors.append(color)
        edge_widths.append(width)

    return edge_colors, edge_widths

# Global geocoder instance (reused across requests)
_geolocator = None

def _get_geolocator():
    """Get or create the singleton Nominatim geolocator instance."""
    global _geolocator
    if _geolocator is None:
        _geolocator = Nominatim(user_agent="CityMapPoster/1.0 (contact: bo.hamilton09@gmail.com)")
    return _geolocator

# Cache for geocoding results (key: "city, country")
_geocode_cache = {}
_last_geocode_time = 0

def get_coordinates(city, country):
    """
    Fetches coordinates for a given city and country using geopy.
    Includes caching and rate limiting to be respectful to the geocoding service.
    """
    global _last_geocode_time

    cache_key = f"{city.lower()}, {country.lower()}"

    # Check cache first
    if cache_key in _geocode_cache:
        print(f"✓ Using cached coordinates for {city}, {country}")
        lat, lon, address = _geocode_cache[cache_key]
        print(f"✓ Found: {address}")
        print(f"✓ Coordinates: {lat}, {lon}")
        return (lat, lon)

    print("Looking up coordinates...")
    geolocator = _get_geolocator()

    # Rate limiting: ensure at least 1 second between API calls
    elapsed = time.time() - _last_geocode_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    location = geolocator.geocode(f"{city}, {country}")
    _last_geocode_time = time.time()

    if location:
        print(f"✓ Found: {location.address}")
        print(f"✓ Coordinates: {location.latitude}, {location.longitude}")

        # Cache the result
        _geocode_cache[cache_key] = (location.latitude, location.longitude, location.address)

        return (location.latitude, location.longitude)
    else:
        raise ValueError(f"Could not find coordinates for {city}, {country}")


def _fetch_street_network(point, dist):
    """Fetch street network data from OSM."""
    return ox.graph_from_point(point, dist=dist, dist_type='bbox', network_type='all')

def _fetch_water_features(point, dist):
    """Fetch water features from OSM."""
    try:
        return ox.features_from_point(point, tags={'natural': 'water', 'waterway': 'riverbank'}, dist=dist)
    except:
        return None

def _fetch_parks(point, dist):
    """Fetch parks and green spaces from OSM."""
    try:
        return ox.features_from_point(point, tags={'leisure': 'park', 'landuse': 'grass'}, dist=dist)
    except:
        return None

def create_poster(city, country, point, dist, output_file, figsize=(12, 16), dpi=300, watermark=False):
    print(f"\nGenerating map for {city}, {country}...")

    # Parallel data fetching with ThreadPoolExecutor
    print("Fetching map data in parallel...")
    G = None
    water = None
    parks = None

    with ThreadPoolExecutor(max_workers=3) as executor:
        # Submit all tasks concurrently
        future_streets = executor.submit(_fetch_street_network, point, dist)
        future_water = executor.submit(_fetch_water_features, point, dist)
        future_parks = executor.submit(_fetch_parks, point, dist)

        # Progress bar for completion tracking
        futures = {
            'Street network': future_streets,
            'Water features': future_water,
            'Parks/green spaces': future_parks
        }

        with tqdm(total=3, desc="Downloading", unit="layer", bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}') as pbar:
            for name, future in futures.items():
                pbar.set_description(f"Downloading {name}")
                result = future.result()  # Wait for completion

                if name == 'Street network':
                    G = result
                elif name == 'Water features':
                    water = result
                elif name == 'Parks/green spaces':
                    parks = result

                pbar.update(1)

    print("✓ All data downloaded successfully!")
    
    # 2. Setup Plot
    print("Rendering map...")
    fig, ax = plt.subplots(figsize=figsize, facecolor=THEME['bg'])
    ax.set_facecolor(THEME['bg'])
    ax.set_position([0, 0, 1, 1])
    
    # 3. Plot Layers
    # Layer 1: Polygons
    if water is not None and not water.empty:
        water.plot(ax=ax, facecolor=THEME['water'], edgecolor='none', zorder=1)
    if parks is not None and not parks.empty:
        parks.plot(ax=ax, facecolor=THEME['parks'], edgecolor='none', zorder=2)
    
    # Layer 2: Roads with hierarchy coloring
    print("Applying road hierarchy colors...")
    edge_colors, edge_widths = get_edge_colors_and_widths_by_type(G)
    
    ox.plot_graph(
        G, ax=ax, bgcolor=THEME['bg'],
        node_size=0,
        edge_color=edge_colors,
        edge_linewidth=edge_widths,
        show=False, close=False
    )
    
    # Layer 3: Gradients (Top and Bottom)
    create_gradient_fade(ax, THEME['gradient_color'], location='bottom', zorder=10)
    create_gradient_fade(ax, THEME['gradient_color'], location='top', zorder=10)
    
    # 4. Typography using Roboto font with responsive sizing
    city_upper = city.upper()
    city_length = len(city_upper)

    # Determine font sizes based on city name length
    if city_length <= 8:
        # Short names: full spacing and large size
        base_size = 60
        city_spacing = "  "
    elif city_length <= 12:
        # Medium names: reduced spacing and size
        base_size = 48
        city_spacing = " "
    elif city_length <= 18:
        # Long names: minimal spacing and smaller size
        base_size = 36
        city_spacing = " "
    else:
        # Very long names: no spacing, wrap if needed
        base_size = 28
        city_spacing = ""

    if FONTS:
        font_main = FontProperties(fname=FONTS['bold'], size=base_size)
        font_sub = FontProperties(fname=FONTS['light'], size=22)
        font_coords = FontProperties(fname=FONTS['regular'], size=14)
    else:
        # Fallback to system fonts
        font_main = FontProperties(family='monospace', weight='bold', size=base_size)
        font_sub = FontProperties(family='monospace', weight='normal', size=22)
        font_coords = FontProperties(family='monospace', size=14)

    # Handle very long city names by wrapping
    if city_length > 20:
        # Split into two lines at a space or middle point
        words = city_upper.split()
        if len(words) > 1:
            # Find best split point (closest to middle)
            mid_point = city_length // 2
            current_len = 0
            split_index = 0
            for i, word in enumerate(words):
                current_len += len(word) + (1 if i > 0 else 0)
                if current_len >= mid_point:
                    split_index = i
                    break

            line1 = " ".join(words[:split_index + 1])
            line2 = " ".join(words[split_index + 1:])
        else:
            # No spaces, split in middle
            mid = len(city_upper) // 2
            line1 = city_upper[:mid]
            line2 = city_upper[mid:]

        # Draw two lines
        ax.text(0.5, 0.15, line1, transform=ax.transAxes,
                color=THEME['text'], ha='center', fontproperties=font_main, zorder=11)
        ax.text(0.5, 0.12, line2, transform=ax.transAxes,
                color=THEME['text'], ha='center', fontproperties=font_main, zorder=11)
        country_y = 0.09
        coords_y = 0.06
        line_y = 0.105
    else:
        # Single line with spacing
        spaced_city = city_spacing.join(list(city_upper))
        ax.text(0.5, 0.14, spaced_city, transform=ax.transAxes,
                color=THEME['text'], ha='center', fontproperties=font_main, zorder=11)
        country_y = 0.10
        coords_y = 0.07
        line_y = 0.125

    # --- BOTTOM TEXT ---
    ax.text(0.5, country_y, country.upper(), transform=ax.transAxes,
            color=THEME['text'], ha='center', fontproperties=font_sub, zorder=11)
    
    lat, lon = point
    coords = f"{lat:.4f}° N / {lon:.4f}° E" if lat >= 0 else f"{abs(lat):.4f}° S / {lon:.4f}° E"
    if lon < 0:
        coords = coords.replace("E", "W")

    ax.text(0.5, coords_y, coords, transform=ax.transAxes,
            color=THEME['text'], alpha=0.7, ha='center', fontproperties=font_coords, zorder=11)

    ax.plot([0.4, 0.6], [line_y, line_y], transform=ax.transAxes,
            color=THEME['text'], linewidth=1, zorder=11)

    # --- ATTRIBUTION (bottom right) ---
    if FONTS:
        font_attr = FontProperties(fname=FONTS['light'], size=8)
    else:
        font_attr = FontProperties(family='monospace', size=8)
    
    ax.text(0.98, 0.02, "© OpenStreetMap contributors", transform=ax.transAxes,
            color=THEME['text'], alpha=0.5, ha='right', va='bottom',
            fontproperties=font_attr, zorder=11)

    # Add watermark if requested (for preview mode)
    if watermark:
        if FONTS:
            watermark_font = FontProperties(fname=FONTS['bold'], size=72)
        else:
            watermark_font = FontProperties(family='monospace', weight='bold', size=72)

        # Calculate watermark color (inverse of background for visibility)
        bg_rgb = mcolors.to_rgb(THEME['bg'])
        # Use text color with low opacity for subtle watermark
        watermark_color = THEME['text']

        # Add diagonal watermark across the center
        ax.text(0.5, 0.5, 'PREVIEW', transform=ax.transAxes,
                color=watermark_color, alpha=0.15, ha='center', va='center',
                fontproperties=watermark_font, zorder=12, rotation=45)

    # 5. Save with optimized PNG compression
    print(f"Saving to {output_file}...")
    plt.savefig(output_file, dpi=dpi, facecolor=THEME['bg'],
                bbox_inches='tight', pad_inches=0,
                pil_kwargs={'optimize': True, 'compress_level': 6})
    plt.close('all')

    # Force garbage collection to free memory
    import gc
    gc.collect()

    print(f"✓ Done! Poster saved as {output_file}")

def print_examples():
    """Print usage examples."""
    print("""
City Map Poster Generator
=========================

Usage:
  python create_map_poster.py --city <city> --country <country> [options]

Examples:
  # Iconic grid patterns
  python create_map_poster.py -c "New York" -C "USA" -t noir -d 12000           # Manhattan grid
  python create_map_poster.py -c "Barcelona" -C "Spain" -t warm_beige -d 8000   # Eixample district grid
  
  # Waterfront & canals
  python create_map_poster.py -c "Venice" -C "Italy" -t blueprint -d 4000       # Canal network
  python create_map_poster.py -c "Amsterdam" -C "Netherlands" -t ocean -d 6000  # Concentric canals
  python create_map_poster.py -c "Dubai" -C "UAE" -t midnight_blue -d 15000     # Palm & coastline
  
  # Radial patterns
  python create_map_poster.py -c "Paris" -C "France" -t pastel_dream -d 10000   # Haussmann boulevards
  python create_map_poster.py -c "Moscow" -C "Russia" -t noir -d 12000          # Ring roads
  
  # Organic old cities
  python create_map_poster.py -c "Tokyo" -C "Japan" -t japanese_ink -d 15000    # Dense organic streets
  python create_map_poster.py -c "Marrakech" -C "Morocco" -t terracotta -d 5000 # Medina maze
  python create_map_poster.py -c "Rome" -C "Italy" -t warm_beige -d 8000        # Ancient street layout
  
  # Coastal cities
  python create_map_poster.py -c "San Francisco" -C "USA" -t sunset -d 10000    # Peninsula grid
  python create_map_poster.py -c "Sydney" -C "Australia" -t ocean -d 12000      # Harbor city
  python create_map_poster.py -c "Mumbai" -C "India" -t contrast_zones -d 18000 # Coastal peninsula
  
  # River cities
  python create_map_poster.py -c "London" -C "UK" -t noir -d 15000              # Thames curves
  python create_map_poster.py -c "Budapest" -C "Hungary" -t copper_patina -d 8000  # Danube split
  
  # List themes
  python create_map_poster.py --list-themes

Options:
  --city, -c        City name (required)
  --country, -C     Country name (required)
  --theme, -t       Theme name (default: feature_based)
  --size, -s        Poster size: small, medium, large, xl (default: medium)
  --distance, -d    Map radius in meters (default: 29000)
  --list-themes     List all available themes

Distance guide:
  4000-6000m   Small/dense cities (Venice, Amsterdam old center)
  8000-12000m  Medium cities, focused downtown (Paris, Barcelona)
  15000-20000m Large metros, full city view (Tokyo, Mumbai)

Poster sizes:
  Small   11 × 17 in  Handouts, clipboards
  Medium  18 × 24 in  Office / hallway
  Large   24 × 36 in  Wall posters
  XL      36 × 48 in  Trade shows, lobbies

Available themes can be found in the 'themes/' directory.
Generated posters are saved to 'posters/' directory.
""")

def list_themes():
    """List all available themes with descriptions."""
    available_themes = get_available_themes()
    if not available_themes:
        print("No themes found in 'themes/' directory.")
        return
    
    print("\nAvailable Themes:")
    print("-" * 60)
    for theme_name in available_themes:
        theme_path = os.path.join(THEMES_DIR, f"{theme_name}.json")
        try:
            with open(theme_path, 'r') as f:
                theme_data = json.load(f)
                display_name = theme_data.get('name', theme_name)
                description = theme_data.get('description', '')
        except:
            display_name = theme_name
            description = ''
        print(f"  {theme_name}")
        print(f"    {display_name}")
        if description:
            print(f"    {description}")
        print()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate beautiful map posters for any city",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python create_map_poster.py --city "New York" --country "USA"
  python create_map_poster.py --city Tokyo --country Japan --theme midnight_blue
  python create_map_poster.py --city Paris --country France --theme noir --distance 15000
  python create_map_poster.py --list-themes
        """
    )
    
    parser.add_argument('--city', '-c', type=str, help='City name')
    parser.add_argument('--country', '-C', type=str, help='Country name')
    parser.add_argument('--theme', '-t', type=str, default='feature_based', help='Theme name (default: feature_based)')
    parser.add_argument(
        '--size',
        '-s',
        type=str,
        default='medium',
        choices=sorted(POSTER_SIZES.keys()),
        help='Poster size (default: medium)'
    )
    parser.add_argument('--distance', '-d', type=int, default=29000, help='Map radius in meters (default: 29000)')
    parser.add_argument('--list-themes', action='store_true', help='List all available themes')
    
    args = parser.parse_args()
    
    # If no arguments provided, show examples
    if len(os.sys.argv) == 1:
        print_examples()
        os.sys.exit(0)
    
    # List themes if requested
    if args.list_themes:
        list_themes()
        os.sys.exit(0)
    
    # Validate required arguments
    if not args.city or not args.country:
        print("Error: --city and --country are required.\n")
        print_examples()
        os.sys.exit(1)
    
    # Validate theme exists
    available_themes = get_available_themes()
    if args.theme not in available_themes:
        print(f"Error: Theme '{args.theme}' not found.")
        print(f"Available themes: {', '.join(available_themes)}")
        os.sys.exit(1)
    
    print("=" * 50)
    print("City Map Poster Generator")
    print("=" * 50)
    
    # Load theme
    THEME = load_theme(args.theme)
    
    # Get coordinates and generate poster
    try:
        coords = get_coordinates(args.city, args.country)
        output_file = generate_output_filename(args.city, args.theme)
        create_poster(args.city, args.country, coords, args.distance, output_file, args.size)
        
        print("\n" + "=" * 50)
        print("✓ Poster generation complete!")
        print("=" * 50)
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        os.sys.exit(1)
