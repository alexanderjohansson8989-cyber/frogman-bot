"""Option mapping for the API contract. Server acceptance is verified separately."""
import copy

def labels(value):
    if (not isinstance(value,list) or len(value)<2 or any(not isinstance(x,str) or not x for x in value)
        or len(set(value))!=len(value)):raise ValueError('Distinct nonempty options required')
    return copy.deepcopy(value)

def option_contract(question):
    active=labels(question.get('options'));history=question.get('options_history');ever=question.get('all_options_ever')
    if history is None or history==[]:
        if ever is None:
            universe=active[:];origin='legacy_current_options_no_history'
        else:universe=labels(ever);origin='explicit_all_options_ever'
    else:
        if not isinstance(history,list):raise ValueError('Invalid option history')
        universe=[];catchall=None
        for entry in history:
            if not isinstance(entry,(list,tuple)) or len(entry)!=2 or not isinstance(entry[0],str):raise ValueError('Invalid history entry')
            row=labels(entry[1])
            if catchall is None:catchall=row[-1]
            if row[-1]!=catchall:raise ValueError('Conflicting catch-all history')
            for label in row[:-1]:
                if label not in universe:universe.append(label)
        universe.append(catchall);origin='reconstructed_history'
        if ever is not None and labels(ever)!=universe:raise ValueError('Conflicting master option order')
    if not set(active)<=set(universe):raise ValueError('Active options missing from master list')
    return dict(active=active,universe=universe,origin=origin,
                raw_all_options_ever=copy.deepcopy(ever),raw_options_history=copy.deepcopy(history))

def expected_values(probabilities,contract):
    if set(probabilities)!=set(contract['active']):raise ValueError('Exact active probabilities required')
    return [probabilities.get(label) for label in contract['universe']]
