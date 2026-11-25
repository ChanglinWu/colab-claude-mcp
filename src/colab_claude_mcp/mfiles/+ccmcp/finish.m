function finish(outDir, prefix, which, closeAfter, resolution, refreshFolder, pdf)
%CCMCP.FINISH End a run from colab-claude-mcp: stop the diary and export figures to PNG.
%   which is 'new' (figures made by the run), 'all' or 'none'. The PNG paths are written,
%   one per line, to <outDir>/<prefix>.figs so that the MCP server can pick them up.
%   pdf also saves each figure as a vector PDF with embedded fonts, next to its PNG.
%   refreshFolder makes the MATLAB desktop show the real current folder again after run():
%   run() returns to the original folder without telling the desktop.
    diary('off');
    if nargin >= 6 && refreshFolder && desktop('-inuse')
        here = pwd;
        parent = fileparts(here);
        if ~strcmp(parent, here)
            try
                cd(parent);
            catch
            end
            cd(here);
        end
    end
    if nargin < 5 || isempty(resolution)
        resolution = 300;
    end
    files = {};
    figs = findall(groot, 'Type', 'figure');
    if ~isempty(figs) && ~strcmp(which, 'none')
        numbers = zeros(1, numel(figs));
        for k = 1:numel(figs)
            n = figs(k).Number;
            if isempty(n), n = inf; end
            numbers(k) = n;
        end
        [~, order] = sort(numbers);
        figs = figs(order);
        for k = 1:numel(figs)
            h = figs(k);
            if strcmp(which, 'new') && isappdata(h, 'ccmcp_seen')
                continue
            end
            file = fullfile(outDir, sprintf('%s_fig%d.png', prefix, numel(files) + 1));
            res = exportResolution(h, resolution);
            try
                exportgraphics(h, file, 'Resolution', res);
            catch
                try
                    print(h, file, '-dpng', sprintf('-r%d', res));
                catch err
                    warning('ccmcp:export', 'Could not export figure: %s', err.message);
                    continue
                end
            end
            files{end + 1} = file; %#ok<AGROW>
            if nargin >= 7 && pdf
                exportPdf(h, [file(1:end - 4) '.pdf']);
            end
        end
    end
    for k = 1:numel(figs)
        if ~isvalid(figs(k)), continue, end
        if closeAfter && (strcmp(which, 'all') || ~isappdata(figs(k), 'ccmcp_seen'))
            close(figs(k));
        else
            setappdata(figs(k), 'ccmcp_seen', true);
        end
    end
    fid = fopen(fullfile(outDir, [prefix '.figs']), 'w');
    if fid > 0
        fprintf(fid, '%s\n', files{:});
        fclose(fid);
    end
end

function res = exportResolution(h, resolution)
% A large figure (docked in the desktop, or full screen) is exported at a lower resolution, so
% that its PNG stays under about 2400 px wide. One-column (3.5 in) figures keep the full value.
    px = getpixelposition(h);
    widthInches = px(3) / get(groot, 'ScreenPixelsPerInch');
    res = max(72, min(resolution, floor(2400 / max(widthInches, 0.5))));
end

function exportPdf(h, file)
% Vector output on purpose: 'auto' turns a figure with interpolated colors (a pdeplot field)
% into one raster image without fonts. MATLAB warns that vector output of such content may be
% slow; that warning is expected here.
    state = warning('off', 'MATLAB:print:ContentTypeImageSuggested');
    try
        exportgraphics(h, file, 'ContentType', 'vector');
    catch err
        warning(state);
        warning('ccmcp:export', 'Could not export figure as PDF: %s', err.message);
        return
    end
    warning(state);
end
