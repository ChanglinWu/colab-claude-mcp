function style(name)
%CCMCP.STYLE Default look of new figures in this MATLAB session.
%   ccmcp.style('ieee')    IEEE Transactions figures (the default in colab-claude-mcp sessions):
%                          - one column wide, 3.5 x 2.6 in (a two-column figure is 7.16 in wide:
%                            figure('Units','inches','Position',[1 1 7.16 3]))
%                          - Times New Roman, 9 pt; axis labels and titles 10 pt (IEEE asks for
%                            type of about 9-10 pt at full size)
%                          - lines told apart by line style as well as by color, so that a
%                            grayscale printout still works: black solid, blue dashed, red
%                            dash-dot, wine dotted, olive solid, pink dashed, ...
%                          - colors checked for color-vision deficiency and >= 3:1 contrast on white
%                          - 1.25 pt data lines, 0.5 pt axes, boxed axes with ticks inside
%                          - the light figure theme, also while the operating system is in dark
%                            mode (MATLAB R2025a and later would otherwise give new figures the
%                            dark theme, on which the black text and axes disappear). This uses
%                            the session-only value of the figure-theme setting; nothing is saved.
%   ccmcp.style('matlab')  MATLAB's own defaults, including the theme that follows the system.
%   ccmcp.style('auto')    'ieee', unless a style was already chosen in this session; then it
%                          only makes sure an 'ieee' session keeps the light theme.
%   Figures that already exist keep their look.
    if nargin < 1
        name = 'ieee';
    end
    name = lower(char(name));
    if strcmp(name, 'auto')
        if isappdata(groot, 'ccmcp_style')
            if strcmp(getappdata(groot, 'ccmcp_style'), 'ieee')
                lightFigures(true);   % sessions styled before the theme handling existed
            end
            return
        end
        name = 'ieee';
    end
    defaults = ieeeDefaults();
    switch name
        case 'ieee'
            for k = 1:2:numel(defaults)
                try
                    set(groot, defaults{k}, defaults{k + 1});
                catch
                    % a property this MATLAB release does not have
                end
            end
            lightFigures(true);
        case 'matlab'
            for k = 1:2:numel(defaults)
                try
                    set(groot, defaults{k}, 'remove');
                catch
                end
            end
            lightFigures(false);
        otherwise
            error('ccmcp:style', 'Unknown style "%s". Use ''ieee'' or ''matlab''.', name);
    end
    setappdata(groot, 'ccmcp_style', name);
end

function lightFigures(on)
%LIGHTFIGURES Give new figures the light theme for this session (on) or the system's again (off).
%   Uses the TemporaryValue of the figure-theme setting, which lasts until MATLAB quits and is
%   never saved. Releases without figure themes (before R2025a) are left alone.
    try
        s = settings;
        group = s.matlab.appearance.figure;
        if ~hasSetting(group, 'GraphicsTheme')
            return
        end
        figureTheme = group.GraphicsTheme;
        if on
            figureTheme.TemporaryValue = "light";
        elseif hasTemporaryValue(figureTheme)
            clearTemporaryValue(figureTheme);
        end
    catch
        % a settings layout this release does not have: keep MATLAB's theme
    end
end

function defaults = ieeeDefaults()
    font = 'Times New Roman';
    if ~any(strcmpi(listfonts, font))
        font = 'Times';
    end
    % MATLAB measures default figure positions in pixels; keep that unit so that code which
    % sets Position in pixels still works, and size the figure from the screen resolution.
    ppi = get(groot, 'ScreenPixelsPerInch');
    position = [100 100 round(3.5 * ppi) round(2.6 * ppi)];
    % Black, then five colors picked for color-vision-deficiency separation (worst pair
    % Delta E 9.6 in OKLab x100) and >= 3:1 contrast on white. Blue and red, and olive and
    % pink, look alike in grayscale; with four line styles cycling together with these six
    % colors, neither pair ever shares a line style.
    colors = [0 0 0; 0 114 178; 204 51 17; 136 34 85; 153 153 51; 204 121 167] / 255;
    pt = 9;  % base type size
    defaults = { ...
        'defaultFigurePosition', position, ...
        'defaultFigureWindowStyle', 'normal', ...  % true size in the desktop, not docked
        'defaultFigureColor', [1 1 1], ...
        'defaultFigurePaperPositionMode', 'auto', ...
        'defaultAxesFontName', font, ...
        'defaultAxesFontSize', pt, ...
        'defaultAxesLabelFontSizeMultiplier', 10 / pt, ...
        'defaultAxesTitleFontSizeMultiplier', 10 / pt, ...
        'defaultAxesTitleFontWeight', 'normal', ...
        'defaultAxesLineWidth', 0.5, ...
        'defaultAxesBox', 'on', ...
        'defaultAxesTickDir', 'in', ...
        'defaultAxesTickLength', [0.02 0.025], ...
        'defaultAxesLayer', 'top', ...
        'defaultAxesXColor', [0 0 0], ...
        'defaultAxesYColor', [0 0 0], ...
        'defaultAxesZColor', [0 0 0], ...
        'defaultAxesGridColor', [0 0 0], ...
        'defaultAxesGridAlpha', 0.15, ...
        'defaultAxesMinorGridColor', [0 0 0], ...
        'defaultAxesMinorGridAlpha', 0.08, ...
        'defaultAxesColorOrder', colors, ...
        'defaultAxesLineStyleOrder', {'-', '--', '-.', ':'}, ...
        'defaultAxesLineStyleCyclingMethod', 'withcolor', ...
        'defaultLineLineWidth', 1.25, ...
        'defaultLineMarkerSize', 5, ...
        'defaultStairLineWidth', 1.25, ...
        'defaultErrorbarLineWidth', 1, ...
        'defaultFunctionlineLineWidth', 1.25, ...
        'defaultScatterSizeData', 20, ...
        'defaultTextFontName', font, ...
        'defaultTextFontSize', pt, ...
        'defaultLegendFontName', font, ...
        'defaultLegendFontSize', pt, ...
        'defaultLegendLineWidth', 0.5, ...
        'defaultLegendItemTokenSize', [24 18], ...
        'defaultColorbarFontName', font, ...
        'defaultColorbarFontSize', pt, ...
        'defaultColorbarLineWidth', 0.5, ...
        'defaultColorbarTickDirection', 'in', ...
        };
end
