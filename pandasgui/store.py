import textwrap
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Union, Iterable, Literal
import pandas as pd
from pandas import DataFrame
from PyQt5 import QtCore, QtGui, QtWidgets, sip
from PyQt5.QtCore import Qt
import traceback
from functools import wraps
from datetime import datetime
from pandasgui.utility import unique_name, in_interactive_console, rename_duplicates, refactor_variable, parse_dates, \
    clean_dataframe, nunique
from pandasgui.constants import LOCAL_DATA_DIR
import os
import collections
from enum import Enum
import json
import inspect
import logging
import re
import contextlib

logger = logging.getLogger(__name__)

# JSON file that stores persistent user preferences
preferences_path = os.path.join(LOCAL_DATA_DIR, 'preferences.json')
if not os.path.exists(preferences_path):
    with open(preferences_path, 'w') as f:
        json.dump({'theme': "light"}, f)


def read_saved_settings():
    if not os.path.exists(preferences_path):
        return {}
    else:
        with open(preferences_path, 'r') as f:
            saved_settings = json.load(f)
        return saved_settings


def write_saved_settings(settings):
    with open(preferences_path, 'w') as f:
        json.dump(settings, f)


class DictLike:
    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)


class Setting(DictLike):
    def __init__(self, label, value, description, dtype, persist):
        self.label: str = label
        self.value: any = value
        self.description: str = description
        self.dtype: Union[type(str), type(bool), Enum] = dtype
        self.persist: bool = persist

    def __setattr__(self, key, value):
        try:
            if self.persist:
                settings = read_saved_settings()
                settings[self.label] = value
                write_saved_settings(settings)
        except AttributeError:
            # Get attribute error because of __setattr__ happening in __init__ before self.persist is set
            pass

        super().__setattr__(key, value)


DEFAULT_SETTINGS = {'editable': True,
                    'block': None,
                    'theme': 'light',
                    'render_mode': 'auto',
                    'aggregation': 'mean',
                    'title_format': "{name}: {title_columns}{title_dimensions}{names}{title_y}{title_z}{over_by}"
                                    "{title_x} {selection}<br><sub>{groupings}{filters} {title_trendline}</sub>"

                    }


@dataclass
class SettingsStore(DictLike, QtCore.QObject):
    settingsChanged = QtCore.pyqtSignal()

    block: Setting
    editable: Setting
    theme: Setting
    render_mode: Setting
    aggregation: Setting
    title_format: Setting

    def __init__(self, **settings):
        super().__init__()

        saved_settings = read_saved_settings()

        for setting_name in DEFAULT_SETTINGS.keys():
            # Fill settings values if not provided
            if setting_name not in settings.keys():
                if setting_name in saved_settings.keys():
                    settings[setting_name] = saved_settings[setting_name]
                else:
                    settings[setting_name] = DEFAULT_SETTINGS[setting_name]

        if in_interactive_console():
            # Don't block if in an interactive console (so you can view GUI and still continue running commands)
            settings['block'] = False
        else:
            # If in a script, block or else the script will continue and finish without allowing GUI interaction
            settings['block'] = True

        self.block = Setting(label="block",
                             value=settings['block'],
                             description="Should GUI block code execution until closed?",
                             dtype=bool,
                             persist=False)

        self.editable = Setting(label="editable",
                                value=settings['editable'],
                                description="Are table cells editable?",
                                dtype=bool,
                                persist=True)

        self.theme = Setting(label="theme",
                             value=settings['theme'],
                             description="UI theme",
                             dtype=Literal['light', 'dark', 'classic'],
                             persist=True)

        # Settings related to Grapher

        self.render_mode = Setting(label="render_mode",
                                   value=settings['render_mode'],
                                   description="render_mode",
                                   dtype=Literal['auto', 'webgl', 'svg'],
                                   persist=True)

        self.aggregation = Setting(label="aggregation",
                                   value=settings['aggregation'],
                                   description="aggregation",
                                   dtype=Literal['mean', 'median', 'min', 'max', 'sum', 'none'],
                                   persist=True)

        self.title_format = Setting(label="title_format",
                                    value=settings['title_format'],
                                    description="title_format",
                                    dtype=dict,
                                    persist=True)

    def reset_to_defaults(self):
        for setting_name, setting_value in DEFAULT_SETTINGS.items():
            self[setting_name].value = setting_value

    def __repr__(self):
        return '\n'.join([f"{key} = {val.value}" for key, val in self.__dict__.items()])


@dataclass
class Filter:
    expr: str
    enabled: bool
    failed: bool


@dataclass
class HistoryItem:
    comment: str
    code: str
    time: str

    def __init__(self, comment, code):
        self.comment = comment
        self.code = code
        self.time = datetime.now().strftime("%H:%M:%S")


# Use this decorator on PandasGuiStore or PandasGuiDataFrameStore to display a status bar message during a method run
def status_message_decorator(message):
    def decorator(function):
        def wrapper(self, *args, **kwargs):

            if not (issubclass(type(self), PandasGuiStore) or issubclass(type(self), PandasGuiDataFrameStore)):
                raise ValueError

            full_kwargs = kwargs.copy()
            # Allow putting method argument values in the status message by putting them in curly braces
            args_spec = inspect.getfullargspec(function).args
            args_spec.pop(0)  # Removes self
            for ix, arg_name in enumerate(args_spec):
                # Need to check length because if the param has default value it may be in args_spec but not args
                if ix < len(args):
                    full_kwargs[arg_name] = args[ix]
            new_message = message

            for arg_name in full_kwargs.keys():
                new_message = new_message.replace('{' + arg_name + '}', str(full_kwargs[arg_name]))

            if self.gui is not None:
                original_status = self.gui.statusBar().currentMessage()
                self.gui.statusBar().showMessage(new_message)
                self.gui.statusBar().repaint()
                QtWidgets.QApplication.instance().processEvents()
                try:
                    result = function(self, *args, **kwargs)
                finally:
                    self.gui.statusBar().showMessage(original_status)
                    self.gui.statusBar().repaint()
                    QtWidgets.QApplication.instance().processEvents()
            else:
                result = function(self, *args, **kwargs)
            return result

        return wrapper

    return decorator


# Use this context to display a status message for a block. self should be a PandasGuiStore or PandasGuiDataFrameStore
@contextlib.contextmanager
def status_message_context(self, message):
    if self.gui is not None:
        original_status = self.gui.statusBar().currentMessage()
        self.gui.statusBar().showMessage(message)
        self.gui.statusBar().repaint()
        QtWidgets.QApplication.instance().processEvents()
        try:
            yield
        finally:
            self.gui.statusBar().showMessage(original_status)
            self.gui.statusBar().repaint()
            QtWidgets.QApplication.instance().processEvents()


class PandasGuiDataFrameStore:
    """
    All methods that modify the data should modify self.df_unfiltered, then self.df gets computed from that
    """

    def __init__(self, df: DataFrame, name: str = 'Untitled'):
        super().__init__()
        df = df.copy()

        self.df: DataFrame = df
        self.df_unfiltered: DataFrame = df
        self.name = name

        self.history: List[HistoryItem] = []
        self.history_imports = {"import pandas as pd"}

        # References to other object instances that may be assigned later
        self.settings: SettingsStore = SETTINGS_STORE
        self.store: Union[PandasGuiStore, None] = None
        self.gui: Union["PandasGui", None] = None
        self.dataframe_explorer: Union["DataFrameExplorer", None] = None
        self.dataframe_viewer: Union["DataFrameViewer", None] = None
        self.stats_viewer: Union["DataFrameViewer", None] = None
        self.filter_viewer: Union["FilterViewer", None] = None

        self.column_sorted: Union[int, None] = None
        self.index_sorted: Union[int, None] = None
        self.sort_is_ascending: Union[bool, None] = None

        self.filters: List[Filter] = []
        self.filtered_index_map = df.reset_index().index

        # Statistics
        self.column_statistics = None
        self.row_statistics = None

        self.data_changed()

    def __setattr__(self, name, value):
        if name == 'df':
            value.pgdf = self
        super().__setattr__(name, value)

    @status_message_decorator("Refreshing statistics...")
    def refresh_statistics(self):

        df = self.df
        self.column_statistics = pd.DataFrame({
            "Type": df.dtypes.astype(str),
            "Count": df.count(),
            "N Unique": nunique(df),
            "Mean": df.mean(numeric_only=True),
            "StdDev": df.std(numeric_only=True),
            "Min": df.min(numeric_only=True),
            "Max": df.max(numeric_only=True),
        }, index=df.columns
        )

        df = self.df.transpose()
        df_numeric = self.df.select_dtypes('number').transpose()
        self.row_statistics = pd.DataFrame({
            # "Type": df.dtypes.astype(str),
            # "Count": df.count(),
            # "N Unique": nunique(df),
            # "Mean": df_numeric.mean(numeric_only=True),
            # "StdDev": df_numeric.std(numeric_only=True),
            # "Min": df_numeric.min(numeric_only=True),
            "Max": df_numeric.max(numeric_only=True),
        }, index=df.columns
        )

    ###################################
    # Code history

    @status_message_decorator("Generating code export...")
    def code_export(self):

        if len(self.history) == 0:
            return f"# No actions have been recorded yet on this DataFrame ({self.name})"

        code_history = "# 'df' refers to the DataFrame passed into 'pandasgui.show'\n\n"

        # Add imports to setup
        code_history += '\n'.join(self.history_imports) + '\n\n'

        for history_item in self.history:
            code_history += f'# {history_item.comment}\n'
            code_history += history_item.code
            code_history += "\n\n"

        if any([filt.enabled for filt in self.filters]):
            code_history += f"# Filters\n"
        for filt in self.filters:
            if filt.enabled:
                code_history += f"df = df.query('{filt.expr}')\n"

        return code_history

    def add_history_item(self, comment, code):
        history_item = HistoryItem(comment, code)
        self.history.append(history_item)

    ###################################
    # Editing cell data

    @status_message_decorator("Applying cell edit...")
    def edit_data(self, row, col, value):
        # Map the row number in the filtered df (which the user interacts with) to the unfiltered one
        row = self.filtered_index_map[row]

        self.df_unfiltered.iat[row, col] = value
        self.apply_filters()

        self.add_history_item("edit_data",
                              f"df.iat[{row}, {col}] = {value}")

    @status_message_decorator("Pasting data...")
    def paste_data(self, top_row, left_col, df_to_paste):
        new_df = self.df_unfiltered.copy()

        # Not using iat here because it won't work with MultiIndex
        for i in range(df_to_paste.shape[0]):
            for j in range(df_to_paste.shape[1]):
                value = df_to_paste.iloc[i, j]
                new_df.at[self.df.index[top_row + i],
                          self.df.columns[left_col + j]] = value

        self.df_unfiltered = new_df
        self.apply_filters()

        self.add_history_item("paste_data", inspect.cleandoc(
            f"""
            df_to_paste = pd.DataFrame({df_to_paste.to_dict(orient='list')})
            for i in range(df_to_paste.shape[0]):
                for j in range(df_to_paste.shape[1]):
                    value = df_to_paste.iloc[i, j]
                    df.at[df.index[{top_row} + i],
                          df.columns[{left_col} + j]] = value
            """))

    ###################################
    # Sorting

    @status_message_decorator("Sorting column...")
    def sort_column(self, ix: int):
        col_name = self.df_unfiltered.columns[ix]

        # Clicked an unsorted column
        if ix != self.column_sorted:
            self.df_unfiltered = self.df_unfiltered.sort_values(col_name, ascending=True, kind='mergesort')
            self.column_sorted = ix
            self.sort_is_ascending = True

            self.add_history_item("sort_column",
                                  f"df = df.sort_values(df.columns[{ix}], ascending=True, kind='mergesort')")

        # Clicked a sorted column
        elif ix == self.column_sorted and self.sort_is_ascending:
            self.df_unfiltered = self.df_unfiltered.sort_values(col_name, ascending=False, kind='mergesort')
            self.column_sorted = ix
            self.sort_is_ascending = False

            self.add_history_item("sort_column",
                                  f"df = df.sort_values(df.columns[{ix}], ascending=False, kind='mergesort')")

        # Clicked a reverse sorted column - reset to sorted by index
        elif ix == self.column_sorted:
            self.df_unfiltered = self.df_unfiltered.sort_index(ascending=True, kind='mergesort')
            self.column_sorted = None
            self.sort_is_ascending = None

            self.add_history_item("sort_column",
                                  "df = df.sort_index(ascending=True, kind='mergesort')")

        self.index_sorted = None
        self.apply_filters()

    @status_message_decorator("Sorting index...")
    def sort_index(self, ix: int):
        # Clicked an unsorted index level
        if ix != self.index_sorted:
            self.df_unfiltered = self.df_unfiltered.sort_index(level=ix, ascending=True, kind='mergesort')
            self.index_sorted = ix
            self.sort_is_ascending = True

            self.add_history_item("sort_index",
                                  f"df = df.sort_index(level={ix}, ascending=True, kind='mergesort')")

        # Clicked a sorted index level
        elif ix == self.index_sorted and self.sort_is_ascending:
            self.df_unfiltered = self.df_unfiltered.sort_index(level=ix, ascending=False, kind='mergesort')
            self.index_sorted = ix
            self.sort_is_ascending = False

            self.add_history_item("sort_index",
                                  f"df = df.sort_index(level={ix}, ascending=False, kind='mergesort')")

        # Clicked a reverse sorted index level - reset to sorted by full index
        elif ix == self.index_sorted:
            self.df_unfiltered = self.df_unfiltered.sort_index(ascending=True, kind='mergesort')

            self.index_sorted = None
            self.sort_is_ascending = None

            self.add_history_item("sort_index",
                                  "df = df.sort_index(ascending=True, kind='mergesort')")

        self.column_sorted = None
        self.apply_filters()

    ###################################
    # Filters

    def any_filtered(self):
        return any(filt.enabled for filt in self.filters)

    def add_filter(self, expr: str, enabled=True):
        filt = Filter(expr=expr, enabled=enabled, failed=False)
        self.filters.append(filt)
        self.apply_filters()

    def remove_filter(self, index: int):
        self.filters.pop(index)
        self.apply_filters()

    def edit_filter(self, index: int, expr: str):
        filt = self.filters[index]
        filt.expr = expr
        filt.failed = False
        self.apply_filters()

    def toggle_filter(self, index: int):
        self.filters[index].enabled = not self.filters[index].enabled
        self.apply_filters()

    @status_message_decorator("Applying filters...")
    def apply_filters(self):
        df = self.df_unfiltered.copy()
        df['_temp_range_index'] = df.reset_index().index

        for ix, filt in enumerate(self.filters):
            if filt.enabled and not filt.failed:
                try:
                    df = df.query(filt.expr)
                except Exception as e:
                    self.filters[ix].failed = True
                    logger.exception(e)

        self.filtered_index_map = df['_temp_range_index'].reset_index(drop=True)
        df = df.drop('_temp_range_index', axis=1)

        self.df = df
        self.data_changed()

    ###################################
    # Other

    def data_changed(self):
        self.refresh_ui()
        self.refresh_statistics()

    # Refresh PyQt models when the underlying pgdf is changed in anyway that needs to be reflected in the GUI
    def refresh_ui(self):

        self.models = []

        if self.filter_viewer is not None:
            self.models += [self.filter_viewer.list_model]

        for model in self.models:
            model.beginResetModel()
            model.endResetModel()

        if self.dataframe_viewer is not None:
            self.dataframe_viewer.refresh_ui()

    @staticmethod
    def cast(df: Union["PandasGuiDataFrameStore", pd.DataFrame, pd.Series, Iterable]):
        if isinstance(df, PandasGuiDataFrameStore):
            return df
        if isinstance(df, pd.DataFrame):
            return PandasGuiDataFrameStore(df.copy())
        elif isinstance(df, pd.Series):
            return PandasGuiDataFrameStore(df.to_frame())
        else:
            try:
                return PandasGuiDataFrameStore(pd.DataFrame(df))
            except:
                raise TypeError(f"Could not convert {type(df)} to DataFrame")


@dataclass
class PandasGuiStore:
    settings: Union["SettingsStore", None] = None
    data: Dict[str, PandasGuiDataFrameStore] = field(default_factory=dict)
    gui: Union["PandasGui", None] = None
    navigator: Union["Navigator", None] = None
    selected_pgdf: Union[PandasGuiDataFrameStore, None] = None

    def __post_init__(self):
        self.settings = SETTINGS_STORE

    ###################################
    # IPython magic
    @status_message_decorator("Executing IPython command...")
    def eval_magic(self, line):
        names_to_update = []
        command = line
        for name in self.data.keys():
            names_to_update.append(name)
            command = refactor_variable(command, name, f"self.data['{name}'].df_unfiltered")

        # print(command)
        exec(command)

        for name in names_to_update:
            self.data[name].apply_filters()
        # self.data[0].df_unfiltered = self.data[0].df_unfiltered[self.data[0].df_unfiltered.HP > 50]
        return line

    ###################################

    @status_message_decorator("Adding DataFrame...")
    def add_dataframe(self, pgdf: Union[DataFrame, PandasGuiDataFrameStore],
                      name: str = "Untitled"):

        name = unique_name(name, self.get_dataframes().keys())
        with status_message_context(self, "Adding DataFrame (Creating DataFrame store)..."):
            pgdf = PandasGuiDataFrameStore.cast(pgdf)
        pgdf.settings = self.settings
        pgdf.name = name
        pgdf.store = self
        pgdf.gui = self.gui

        with status_message_context(self, "Adding DataFrame (Cleaning DataFrame)..."):
            pgdf.df = clean_dataframe(pgdf.df, name)

        # Add it to store and create widgets
        self.data[name] = pgdf
        if pgdf.dataframe_explorer is None:
            from pandasgui.widgets.dataframe_explorer import DataFrameExplorer
            pgdf.dataframe_explorer = DataFrameExplorer(pgdf)
        dfe = pgdf.dataframe_explorer
        self.gui.stacked_widget.addWidget(dfe)

        # Add to nav
        shape = pgdf.df.shape
        shape = str(shape[0]) + " X " + str(shape[1])

        item = QtWidgets.QTreeWidgetItem(self.navigator, [name, shape])
        self.navigator.itemSelectionChanged.emit()
        self.navigator.setCurrentItem(item)
        self.navigator.apply_tree_settings()

    def remove_dataframe(self, name):
        self.data.pop(name)
        self.gui.navigator.remove_item(name)

    def rename_dataframe(self, name, new_name):
        pass

    @status_message_decorator('Importing file "{path}"...')
    def import_file(self, path):
        if not os.path.isfile(path):
            logger.warning("Path is not a file: " + path)
        elif path.endswith(".csv"):
            filename = os.path.split(path)[1].split('.csv')[0]
            df = pd.read_csv(path, engine='python')
            self.add_dataframe(df, filename)
        elif path.endswith(".xlsx"):
            filename = os.path.split(path)[1].split('.csv')[0]
            df_dict = pd.read_excel(path, sheet_name=None)
            for sheet_name in df_dict.keys():
                df_name = f"{filename} - {sheet_name}"
                self.add_dataframe(df_dict[sheet_name], df_name)
        elif path.endswith(".parquet"):
            filename = os.path.split(path)[1].split('.parquet')[0]
            df = pd.read_parquet(path, engine='pyarrow')
            self.add_dataframe(df, filename)

        else:
            logger.warning("Can only import csv / xlsx / parquet. Invalid file: " + path)

    def get_pgdf(self, name):
        return self.data[name]

    def get_dataframes(self, names: Union[None, str, list] = None):
        if type(names) == str:
            return self.data[names].df

        df_dict = {}
        for pgdf in self.data.values():
            if names is None or pgdf.name in names:
                df_dict[pgdf.name] = pgdf.df

        return df_dict

    def select_pgdf(self, name):
        pgdf = self.get_pgdf(name)
        dfe = pgdf.dataframe_explorer
        self.gui.stacked_widget.setCurrentWidget(dfe)
        self.selected_pgdf = pgdf

    def to_dict(self):
        import json
        return json.loads(json.dumps(self, default=lambda o: o.__dict__))


SETTINGS_STORE = SettingsStore()
